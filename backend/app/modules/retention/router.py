import json
from collections.abc import Awaitable, Callable, Mapping
from hashlib import sha256
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.idempotency.models import IdempotencyState
from app.modules.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyLeaseLost,
    IdempotencyService,
)
from app.modules.identity.dependencies import (
    Principal,
    get_db_session,
    require_staff_csrf,
    require_staff_session,
)
from app.modules.retention.models import ErasureRequest, ErasureScope, RetentionPolicy
from app.modules.retention.service import (
    ErasureService,
    RetentionAuthorizationError,
    RetentionConflict,
    RetentionService,
    subject_key_hash,
)

router = APIRouter(prefix="/api/v1/admin", tags=["retention"])


class RetentionPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    chat_days: int
    email_days: int
    audit_days: int
    version: int
    legal_compliance_guarantee: Literal[False] = False


class RetentionPolicyPatch(BaseModel):
    expected_version: int = Field(ge=1)
    chat_days: int = Field(ge=1)
    email_days: int = Field(ge=1)
    audit_days: int = Field(ge=1)


class ErasureRequestCreate(BaseModel):
    subject_ref: str = Field(min_length=1, max_length=1024)
    scope: ErasureScope


class ErasureRequestRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope: ErasureScope
    status: str
    replay_generation: int
    verification_counts: dict[str, object]


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    return value.strip()


async def _authorized_policy(db_session: AsyncSession, principal: Principal) -> RetentionPolicy:
    try:
        return await RetentionService(db_session).policy(principal)
    except (RetentionAuthorizationError, LookupError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


async def _idempotent_action[ModelT: BaseModel](
    *,
    db_session: AsyncSession,
    principal: Principal,
    key: str,
    operation: str,
    object_id: UUID,
    request_body: Mapping[str, object],
    response_type: type[ModelT],
    invoke: Callable[[], Awaitable[ModelT]],
    response_status: int,
) -> ModelT:
    idempotency = IdempotencyService(db_session)
    try:
        record = await idempotency.begin(
            scope_id=principal.organization_id,
            actor_id=principal.subject_id,
            operation=operation,
            object_id=object_id,
            key=key,
            request_hash=sha256(
                json.dumps(request_body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        if record.state is IdempotencyState.COMPLETED:
            return response_type.model_validate(record.response_body)
        response = await invoke()
        response_body = response.model_dump(mode="json")
        await idempotency.complete(
            record.id,
            response_status,
            response_body,
            lease_token=record.lease_token,
            safe_response_keys=set(response_body),
        )
        await db_session.commit()
        return response
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except RetentionConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"version": error.version},
        ) from None
    except (RetentionAuthorizationError, LookupError):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


@router.get("/retention-policy", response_model=RetentionPolicyRead)
async def retention_policy(
    principal: Annotated[Principal, Depends(require_staff_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> RetentionPolicyRead:
    policy = await _authorized_policy(db_session, principal)
    return RetentionPolicyRead.model_validate(policy)


@router.patch("/retention-policy", response_model=RetentionPolicyRead)
async def update_retention_policy(
    payload: RetentionPolicyPatch,
    principal: Annotated[Principal, Depends(require_staff_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> RetentionPolicyRead:
    policy = await _authorized_policy(db_session, principal)
    body = payload.model_dump(mode="json")

    async def invoke() -> RetentionPolicyRead:
        updated = await RetentionService(db_session).update_policy(
            principal,
            expected_version=payload.expected_version,
            chat_days=payload.chat_days,
            email_days=payload.email_days,
            audit_days=payload.audit_days,
        )
        return RetentionPolicyRead.model_validate(updated)

    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="retention.policy.update",
        object_id=policy.id,
        request_body=body,
        response_type=RetentionPolicyRead,
        invoke=invoke,
        response_status=status.HTTP_200_OK,
    )


@router.post(
    "/erasure-requests",
    response_model=ErasureRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_erasure_request(
    payload: ErasureRequestCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_staff_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> ErasureRequestRead:
    await _authorized_policy(db_session, principal)
    hash_key = request.app.state.settings.erasure_hash_key
    if hash_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="erasure hashing is not configured",
        )
    digest = subject_key_hash(hash_key.get_secret_value().encode(), payload.subject_ref)
    operation_id = uuid5(
        NAMESPACE_URL,
        f"retention-erasure:{principal.organization_id}:{digest}:{payload.scope.value}",
    )
    body = {"subject_key_hash": digest, "scope": payload.scope.value}

    async def invoke() -> ErasureRequestRead:
        created: ErasureRequest = await ErasureService(
            db_session,
            principal=principal,
            hash_key=hash_key.get_secret_value().encode(),
        ).request(payload.subject_ref, payload.scope)
        return ErasureRequestRead.model_validate(created)

    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="retention.erasure.request",
        object_id=operation_id,
        request_body=body,
        response_type=ErasureRequestRead,
        invoke=invoke,
        response_status=status.HTTP_202_ACCEPTED,
    )
