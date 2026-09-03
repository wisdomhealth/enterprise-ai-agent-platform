from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
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
from app.modules.webhooks.delivery import WebhookSubscriptionService, WebhookVersionConflict
from app.modules.webhooks.models import WebhookSubscriptionStatus

router = APIRouter(prefix="/api/v1/admin/webhooks", tags=["webhooks"])


class WebhookSubscriptionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_url: str = Field(min_length=1, max_length=2048)
    event_types: list[str] = Field(min_length=1, max_length=50)
    signing_secret: SecretStr


class WebhookSubscriptionDisable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class WebhookSubscriptionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    endpoint_url: str
    event_types: list[str]
    status: WebhookSubscriptionStatus
    version: int


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    return value.strip()


def _service(request: Request, db_session: AsyncSession) -> WebhookSubscriptionService:
    return WebhookSubscriptionService(
        db_session,
        getattr(request.app.state, "webhook_cipher", None),
    )


async def _idempotent_action(
    *,
    db_session: AsyncSession,
    principal: Principal,
    key: str,
    operation: str,
    object_id: UUID,
    request_body: dict[str, object],
    response_status: int,
    invoke: Callable[[], Awaitable[WebhookSubscriptionRead]],
) -> WebhookSubscriptionRead:
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
            return WebhookSubscriptionRead.model_validate(record.response_body)
        response = await invoke()
        body = response.model_dump(mode="json")
        await idempotency.complete(
            record.id,
            response_status,
            body,
            lease_token=record.lease_token,
            safe_response_keys=set(body),
        )
        await db_session.commit()
        return response
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except WebhookVersionConflict as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"version": error.version},
        ) from None
    except LookupError:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from None
    except RuntimeError as error:
        await db_session.rollback()
        if str(error) == "webhook envelope encryption is not configured":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from None
        raise


@router.get("/subscriptions", response_model=list[WebhookSubscriptionRead])
async def list_subscriptions(
    request: Request,
    principal: Annotated[Principal, Depends(require_staff_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[WebhookSubscriptionRead]:
    return [
        WebhookSubscriptionRead.model_validate(subscription)
        for subscription in await _service(request, db_session).list_authorized(principal)
    ]


@router.post(
    "/subscriptions",
    response_model=WebhookSubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_subscription(
    payload: WebhookSubscriptionCreate,
    request: Request,
    principal: Annotated[Principal, Depends(require_staff_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> WebhookSubscriptionRead:
    subscription_id = uuid5(
        NAMESPACE_URL,
        f"webhook-subscription:{principal.organization_id}:{idempotency_key}",
    )
    signing_secret = payload.signing_secret.get_secret_value()
    body: dict[str, object] = {
        "endpoint_url": payload.endpoint_url,
        "event_types": payload.event_types,
        "signing_secret_sha256": sha256(signing_secret.encode()).hexdigest(),
    }

    async def invoke() -> WebhookSubscriptionRead:
        subscription = await _service(request, db_session).create(
            principal,
            subscription_id=subscription_id,
            endpoint_url=payload.endpoint_url,
            event_types=payload.event_types,
            signing_secret=signing_secret,
        )
        return WebhookSubscriptionRead.model_validate(subscription)

    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="webhook.subscription.create",
        object_id=subscription_id,
        request_body=body,
        response_status=status.HTTP_201_CREATED,
        invoke=invoke,
    )


@router.post(
    "/subscriptions/{subscription_id}/disable",
    response_model=WebhookSubscriptionRead,
)
async def disable_subscription(
    subscription_id: UUID,
    payload: WebhookSubscriptionDisable,
    request: Request,
    principal: Annotated[Principal, Depends(require_staff_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> WebhookSubscriptionRead:
    async def invoke() -> WebhookSubscriptionRead:
        subscription = await _service(request, db_session).disable(
            principal,
            subscription_id,
            expected_version=payload.expected_version,
        )
        return WebhookSubscriptionRead.model_validate(subscription)

    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="webhook.subscription.disable",
        object_id=subscription_id,
        request_body={"expected_version": payload.expected_version},
        response_status=status.HTTP_200_OK,
        invoke=invoke,
    )
