import json
from collections.abc import Awaitable, Callable
from hashlib import sha256
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.application import ApplicationCapability
from app.modules.connectors.models import ConnectorKind
from app.modules.connectors.service import ConnectorService
from app.modules.email.models import EmailSyncState
from app.modules.email.recovery import GmailRecoveryService
from app.modules.email.watch import GmailWatchService
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
from app.modules.jobs.models import JobIntent
from app.modules.operations.application_authorization import ApplicationAuthorizationService
from app.modules.operations.connector_authorization import ConnectorAuthorizationService
from app.modules.operations.schemas import (
    ApplicationAccessRead,
    ApplicationGrantReplace,
    ConnectorAuthorizationRead,
    ConnectorGrantReplace,
    ConnectorReauthorizationRead,
    FailedJobRead,
    GmailManualSyncRequest,
    GmailRecoveryAcceptRequest,
    GmailRecoveryContinueRequest,
    GmailRecoveryResumeRequest,
    GmailSyncStateRead,
    JobRetryRead,
    JobRetryRequest,
    OperationsSummaryRead,
)
from app.modules.operations.service import (
    DeliveryReconciliationOnly,
    OperationsNotFound,
    OperationsService,
)

router = APIRouter(prefix="/api/v1/admin", tags=["administrator-operations"])
staff_router = APIRouter(prefix="/api/v1/staff", tags=["staff-application-access"])


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


@staff_router.get(
    "/application-access",
    response_model=list[ApplicationAccessRead],
)
async def own_application_access(
    principal: Annotated[Principal, Depends(require_staff_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ApplicationAccessRead]:
    return await ApplicationAuthorizationService(db_session).list_for(
        principal, principal.subject_id
    )


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


def _gmail_watch_service(
    request: Request, db_session: AsyncSession
) -> GmailWatchService:
    connector_service = _connector_service(request)
    gateway_factory = getattr(request.app.state, "gmail_gateway_factory", None)
    settings = request.app.state.settings
    if (
        not settings.gmail_push_enabled
        or gateway_factory is None
        or settings.gmail_pubsub_topic is None
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gmail push is not configured",
        )
    return GmailWatchService(
        db_session,
        connector_service=connector_service,
        gateway_factory=gateway_factory,
        topic_name=settings.gmail_pubsub_topic,
    )


async def _gmail_sync_state_read(
    db_session: AsyncSession, state: EmailSyncState, settings: object
) -> GmailSyncStateRead:
    active_job = (
        await db_session.get(JobIntent, state.active_sync_job_id)
        if state.active_sync_job_id is not None
        else None
    )
    return GmailSyncStateRead(
        connector_id=state.connector_id,
        mailbox_address=state.mailbox_address,
        auto_sync_enabled=state.auto_sync_enabled,
        polling_enabled=bool(getattr(settings, "gmail_polling_enabled", False)),
        push_enabled=bool(getattr(settings, "gmail_push_enabled", False)),
        watch_status=state.watch_status.value,
        sync_status=state.sync_status.value,
        processed_history_id=state.processed_history_id,
        pending_history_id=state.pending_history_id,
        watch_expiration_at=state.watch_expiration_at,
        recovery_batch_id=state.recovery_batch_id,
        recovery_window_started_at=state.recovery_window_started_at,
        recovery_processed_count=state.recovery_processed_count,
        recovery_message_limit=state.recovery_message_limit,
        recovery_has_more=state.recovery_has_more,
        last_error_code=state.watch_last_error_code or state.last_error_code,
        last_success_at=state.last_success_at,
        active_job_id=active_job.id if active_job is not None else None,
        active_job_state=active_job.state.value if active_job is not None else None,
        active_job_trigger=(
            str(active_job.payload.get("trigger")) if active_job is not None else None
        ),
        version=state.version,
    )


async def _watch_action(
    action: Awaitable[object], db_session: AsyncSession, settings: object
) -> GmailSyncStateRead:
    try:
        state = await action
        if not isinstance(state, EmailSyncState):
            raise TypeError("Gmail action returned an invalid state")
        return await _gmail_sync_state_read(db_session, state, settings)
    except LookupError:
        raise OperationsNotFound from None


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
    except ValueError as error:
        await db_session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
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


@router.get("/gmail-sync", response_model=list[GmailSyncStateRead])
async def gmail_sync_states(
    request: Request,
    principal: Annotated[Principal, Depends(require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[GmailSyncStateRead]:
    states = list(
        (
            await db_session.scalars(
                select(EmailSyncState)
                .where(EmailSyncState.organization_id == principal.organization_id)
                .order_by(EmailSyncState.created_at)
            )
        ).all()
    )
    return [
        await _gmail_sync_state_read(db_session, state, request.app.state.settings)
        for state in states
    ]


@router.get("/authorization/connectors", response_model=list[ConnectorAuthorizationRead])
async def connector_authorizations(
    principal: Annotated[Principal, Depends(require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ConnectorAuthorizationRead]:
    return await ConnectorAuthorizationService(db_session).list_for(principal)


@router.get(
    "/authorization/applications/{staff_user_id}",
    response_model=list[ApplicationAccessRead],
)
async def application_authorizations(
    staff_user_id: UUID,
    principal: Annotated[Principal, Depends(require_admin_session)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[ApplicationAccessRead]:
    return await ApplicationAuthorizationService(db_session).list_for(
        principal, staff_user_id
    )


@router.put(
    "/authorization/applications/{staff_user_id}/{capability}",
    response_model=ApplicationAccessRead,
)
async def replace_application_grant(
    staff_user_id: UUID,
    capability: ApplicationCapability,
    request_body: ApplicationGrantReplace,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> ApplicationAccessRead:
    service = ApplicationAuthorizationService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.application_grant.replace",
        object_id=uuid5(
            NAMESPACE_URL,
            f"application-grant:{principal.organization_id}:{staff_user_id}:{capability.value}",
        ),
        request_body={
            **request_body.model_dump(mode="json"),
            "staff_user_id": str(staff_user_id),
            "capability": capability.value,
        },
        response_type=ApplicationAccessRead,
        invoke=lambda: service.replace(
            principal,
            target_staff_user_id=staff_user_id,
            capability=capability,
            granted=request_body.granted,
            expected_user_version=request_body.expected_user_version,
        ),
    )


@router.put(
    "/authorization/connectors/{kind}/grants",
    response_model=ConnectorAuthorizationRead,
)
async def replace_connector_grants(
    kind: ConnectorKind,
    request_body: ConnectorGrantReplace,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> ConnectorAuthorizationRead:
    resource_id = ConnectorService.configuration_resource_id(principal.organization_id, kind)
    service = ConnectorAuthorizationService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.connector_grants.replace",
        object_id=resource_id,
        request_body=request_body.model_dump(mode="json"),
        response_type=ConnectorAuthorizationRead,
        invoke=lambda: service.replace(
            principal,
            target_staff_user_id=request_body.staff_user_id,
            kind=kind,
            actions=request_body.actions,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-watch/start",
    response_model=GmailSyncStateRead,
)
async def start_gmail_watch(
    connector_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = _gmail_watch_service(request, db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_watch.start",
        object_id=connector_id,
        request_body={"action": "start"},
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.start(connector_id, principal.organization_id),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-watch/renew",
    response_model=GmailSyncStateRead,
)
async def renew_gmail_watch(
    connector_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = _gmail_watch_service(request, db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_watch.renew",
        object_id=connector_id,
        request_body={"action": "renew"},
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.renew(connector_id, principal.organization_id),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-watch/stop",
    response_model=GmailSyncStateRead,
)
async def stop_gmail_watch(
    connector_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = _gmail_watch_service(request, db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_watch.stop",
        object_id=connector_id,
        request_body={"action": "stop"},
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.stop(connector_id, principal.organization_id),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-recovery/continue",
    response_model=GmailSyncStateRead,
)
async def continue_gmail_recovery(
    connector_id: UUID,
    request: Request,
    request_body: GmailRecoveryContinueRequest,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = GmailRecoveryService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_recovery.continue",
        object_id=connector_id,
        request_body=request_body.model_dump(mode="json"),
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.continue_recovery(
                principal,
                connector_id,
                expected_version=request_body.expected_version,
                additional_messages=request_body.additional_messages,
                reason=request_body.reason,
            ),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-recovery/accept-limited",
    response_model=GmailSyncStateRead,
)
async def accept_limited_gmail_recovery(
    connector_id: UUID,
    request: Request,
    request_body: GmailRecoveryAcceptRequest,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = GmailRecoveryService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_recovery.accept_limited",
        object_id=connector_id,
        request_body=request_body.model_dump(mode="json"),
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.accept_limited_recovery(
                principal,
                connector_id,
                expected_version=request_body.expected_version,
                confirmed=request_body.confirmed,
                reason=request_body.reason,
            ),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-recovery/resume",
    response_model=GmailSyncStateRead,
)
async def resume_gmail_sync(
    connector_id: UUID,
    request: Request,
    request_body: GmailRecoveryResumeRequest,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = GmailRecoveryService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_recovery.resume",
        object_id=connector_id,
        request_body=request_body.model_dump(mode="json"),
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.resume_sync(
                principal,
                connector_id,
                expected_version=request_body.expected_version,
                reason=request_body.reason,
            ),
            db_session,
            request.app.state.settings,
        ),
    )


@router.post(
    "/connectors/{connector_id}/gmail-sync/manual",
    response_model=GmailSyncStateRead,
)
async def manual_gmail_sync(
    connector_id: UUID,
    request: Request,
    request_body: GmailManualSyncRequest,
    principal: Annotated[Principal, Depends(require_admin_csrf)],
    db_session: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[str, Depends(_write_key)],
) -> GmailSyncStateRead:
    service = GmailRecoveryService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.gmail_sync.manual",
        object_id=connector_id,
        request_body=request_body.model_dump(mode="json"),
        response_type=GmailSyncStateRead,
        invoke=lambda: _watch_action(
            service.manual_sync(
                principal,
                connector_id,
                expected_version=request_body.expected_version,
                reason=request_body.reason,
            ),
            db_session,
            request.app.state.settings,
        ),
    )


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
    request_body: JobRetryRequest | None = None,
) -> JobRetryRead:
    service = OperationsService(db_session)
    return await _idempotent_action(
        db_session=db_session,
        principal=principal,
        key=idempotency_key,
        operation="admin.job.retry",
        object_id=job_id,
        request_body=(
            request_body.model_dump(mode="json") if request_body is not None else {}
        ),
        response_type=JobRetryRead,
        invoke=lambda: service.retry_job(
            principal,
            job_id,
            reason=request_body.reason if request_body is not None else None,
            expected_version=(
                request_body.expected_version if request_body is not None else None
            ),
        ),
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
