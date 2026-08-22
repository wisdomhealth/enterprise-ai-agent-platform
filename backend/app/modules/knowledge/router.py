from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.connectors.service import ConnectorService
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_csrf
from app.modules.knowledge.operations import DriveSyncOperations
from app.modules.knowledge.schemas import (
    DriveSourceConfigure,
    DriveSourceRead,
    DriveSyncEnqueued,
    DriveSyncStatusRead,
)
from app.modules.knowledge.service import KnowledgeSourceService

router = APIRouter(prefix="/api/v1/admin/knowledge-sources", tags=["knowledge-sources"])


def _knowledge_source_service(request: Request) -> KnowledgeSourceService:
    connector_service = getattr(request.app.state, "connector_service", None)
    gateway_factory = getattr(request.app.state, "drive_gateway_factory", None)
    if not isinstance(connector_service, ConnectorService) or gateway_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Drive read-only connector is not configured",
        )
    return KnowledgeSourceService(connector_service, gateway_factory)


def _drive_sync_operations(
    db_session: AsyncSession = Depends(get_db_session),
    service: KnowledgeSourceService = Depends(_knowledge_source_service),
) -> DriveSyncOperations:
    return DriveSyncOperations(db_session, service)


@router.put("/drive", response_model=DriveSourceRead)
async def configure_drive_source(
    payload: DriveSourceConfigure,
    db_session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_staff_csrf),
    service: KnowledgeSourceService = Depends(_knowledge_source_service),
) -> DriveSourceRead:
    source = await service.configure_drive_source(
        db_session,
        principal=principal,
        root_folder_id=payload.root_folder_id,
        include_descendants=payload.include_descendants,
    )
    await db_session.commit()
    return DriveSourceRead.model_validate(source)


@router.post(
    "/{source_id}/sync",
    response_model=DriveSyncEnqueued,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_drive_sync(
    source_id: UUID,
    db_session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(require_staff_csrf),
    operations: DriveSyncOperations = Depends(_drive_sync_operations),
) -> DriveSyncEnqueued:
    enqueued = await operations.enqueue_sync_for_dispatch(principal=principal, source_id=source_id)
    await db_session.commit()
    from app.modules.knowledge.tasks import dispatch_drive_sync_outbox_event

    if enqueued.outbox_event_id is not None:
        dispatch_drive_sync_outbox_event.delay(str(enqueued.outbox_event_id))
    return DriveSyncEnqueued(job_id=enqueued.job.id, state=enqueued.job.state.value)


@router.get("/{source_id}/status", response_model=DriveSyncStatusRead)
async def drive_sync_status(
    source_id: UUID,
    principal: Principal = Depends(require_staff_csrf),
    operations: DriveSyncOperations = Depends(_drive_sync_operations),
) -> DriveSyncStatusRead:
    sync_status = await operations.status(principal=principal, source_id=source_id)
    return DriveSyncStatusRead(
        source_id=sync_status.source_id,
        cursor=sync_status.cursor,
        source_status=sync_status.source_status,
        last_success_at=sync_status.last_success_at,
        backlog=sync_status.backlog,
        isolated_files=sync_status.isolated_files,
        retry_count=sync_status.retry_count,
        recent_error_codes=sync_status.recent_error_codes,
    )
