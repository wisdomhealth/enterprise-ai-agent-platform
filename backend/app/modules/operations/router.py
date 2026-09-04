import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.service import ConnectorService
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
from app.modules.operations.schemas import (
    ConnectorReauthorizationRead,
    FailedJobRead,
    JobRetryRead,
    OperationsSummaryRead,
)
from app.modules.operations.service import (
    DeliveryReconciliationOnly,
    OperationsNotFound,
    OperationsService,
)

router = APIRouter(prefix="/api/v1/admin", tags=["administrator-operations"])


async def require_admin_session(
    principal: Principal = Depends(require_staff_session),
) -> Principal:
    try:
        OperationsService.require_admin(principal)
    except OperationsNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return principal


async def require_admin_csrf(
    principal: Principal = Depends(require_staff_csrf),
) -> Principal:
    try:
        OperationsService.require_admin(principal)
    except OperationsNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    return principal


def _write_key(value: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )
    return value.strip()


def _connector_service(request: Request) -> ConnectorService:
    service = getattr(request.app.state, "connector_service", None)
    if not isinstance(service, ConnectorService):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="connector encryption is not configured",
        )
    return service


async def _idempotent_action[ModelT: BaseModel](
    *,
    db_session: AsyncSession,
    principal: Principal,
    key: str,
    operation: str,
    object_id: UUID,
    request_body: dict[str, object],
    response_type: type[ModelT],
    invoke: Callable[[], Awaitable[ModelT]],
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
        body = response.model_dump(mode="json")
        await idempotency.complete(
            record.id,
            status.HTTP_200_OK,
            body,
            lease_token=record.lease_token,
            safe_response_keys=set(body),
        )
        await db_session.commit()
        return response
    except (IdempotencyConflict, IdempotencyInProgress, IdempotencyLeaseLost):
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT) from None
    except OperationsNotFound:
        await db_session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except DeliveryReconciliationOnly as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "DELIVERY_RECONCILIATION_REQUIRED",
                "delivery_intent_id": str(error.delivery_intent_id),
            },
        ) from None
    except HTTPException:
        await db_session.rollback()
        raise


@router.get("/operations/summary", response_model=OperationsSummaryRead)
async def operations_summary(
    principal: Annotated[Principal, Depends(require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> OperationsSummaryRead:
    return await OperationsService(db_session).summary(principal)


@router.get("/jobs/failed", response_model=list[FailedJobRead])
async def failed_jobs(
    principal: Annotated[Principal, Depends(require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[FailedJobRead]:
    return await OperationsService(db_session).failed_jobs(principal)


@router.post(
    "/jobs/{job_id}/retry",
    response_model=JobRetryRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_job(
    job_id: UUID,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> JobRetryRead:
    service = OperationsService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.job.retry",
        object_id=job_id,
        request_body={},
        response_type=JobRetryRead,
        invoke=lambda: service.retry_job(principal, job_id),
    )


@router.post(
    "/connectors/{connector_id}/reauthorize",
    response_model=ConnectorReauthorizationRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reauthorize_connector(
    connector_id: UUID,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    connector_service: Annotated[ConnectorService, Depends(_connector_service)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> ConnectorReauthorizationRead:
    service = OperationsService(db_session, connector_service=connector_service)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.connector.reauthorize",
        object_id=connector_id,
        request_body={},
        response_type=ConnectorReauthorizationRead,
        invoke=lambda: service.begin_connector_reauthorization(principal, connector_id),
    )
