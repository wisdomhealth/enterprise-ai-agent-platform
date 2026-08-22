"""Executable Celery consumer for durable Drive synchronization intents."""

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker
from app.modules.connectors.service import ConnectorService
from app.modules.jobs.models import ErrorClass, JobIntent
from app.modules.jobs.service import JobLeaseLost, JobLeaseService
from app.modules.knowledge.drive_gateway import GoogleDriveGatewayFactory
from app.modules.knowledge.models import Document, DocumentVersion, DriveSource, DriveSourceStatus
from app.modules.knowledge.operations import enqueue_drive_sync_intent
from app.modules.knowledge.sync import DriveSyncService
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService

DRIVE_SYNC_TASK_NAME = "app.modules.knowledge.tasks.drive_source_sync"
DRIVE_SYNC_WORKER_ID = "celery-drive-sync"
DRIVE_SYNC_LEASE_SECONDS = 300
DRIVE_SYNC_OUTBOX_CONSUMER = "celery-drive-sync-dispatcher"


class DocumentParseTask:
    """Task boundary used by workers after a durable document-parse job has been claimed."""

    def __init__(
        self,
        parse_job: Callable[[JobIntent, Document], Awaitable[DocumentVersion]],
    ) -> None:
        self._parse_job = parse_job

    async def run(self, job: JobIntent, document: Document) -> DocumentVersion:
        return await self._parse_job(job, document)


def document_parse_job_key(document_id: UUID, content_sha256: str) -> str:
    return f"document-parse:{document_id}:{content_sha256}"


@shared_task(name=DRIVE_SYNC_TASK_NAME)  # type: ignore[untyped-decorator]
def drive_source_sync(job_id: str | None = None) -> None:
    """Consume one intent, or enqueue+consume the periodic source sweep."""
    asyncio.run(_run_drive_sync(job_id))


@shared_task(name="app.modules.knowledge.tasks.dispatch_drive_sync_outbox_event")  # type: ignore[untyped-decorator]
def dispatch_drive_sync_outbox_event(event_id: str) -> None:
    """At-least-once outbox delivery into the one Drive sync job consumer."""
    asyncio.run(_dispatch_drive_sync_outbox_event(UUID(event_id)))


async def _dispatch_drive_sync_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_drive_sync_outbox_event(event_id, db_session=owned_session)
    event = await db_session.get(OutboxEvent, event_id)
    if event is None or event.event_type != "knowledge.drive_source.sync.requested":
        return False
    accepted = await OutboxService().begin_processing(
        db_session, DRIVE_SYNC_OUTBOX_CONSUMER, event.event_id
    )
    if not accepted:
        await db_session.commit()
        return False
    job_id = event.aggregate_id
    await db_session.commit()
    drive_source_sync.delay(str(job_id))
    return True


async def _run_drive_sync(job_id: str | None) -> None:
    if job_id is not None:
        await _consume_drive_sync_intent(UUID(job_id))
        return
    async with async_sessionmaker() as db_session:
        source_ids = list(
            (
                await db_session.scalars(
                    select(DriveSource.id).where(DriveSource.status == DriveSourceStatus.ACTIVE)
                )
            ).all()
        )
        intent_ids: list[UUID] = []
        for source_id in source_ids:
            source = await db_session.get(DriveSource, source_id)
            if source is None:
                continue
            intent = await enqueue_drive_sync_intent(db_session, source)
            intent_ids.append(intent.job.id)
        await db_session.commit()
    for intent_id in intent_ids:
        await _consume_drive_sync_intent(intent_id)


async def _consume_drive_sync_intent(job_id: UUID) -> None:
    """The only consumer path for scheduled and manual sync intents."""
    settings = Settings()
    connector_service = ConnectorService.from_settings(settings)
    gateway_factory = GoogleDriveGatewayFactory.from_settings(settings)
    if connector_service is None or gateway_factory is None:
        raise RuntimeError("Google Drive connector credentials are not configured")
    async with async_sessionmaker() as db_session:
        lease_service = JobLeaseService(db_session)
        job = await lease_service.claim(job_id, DRIVE_SYNC_WORKER_ID, DRIVE_SYNC_LEASE_SECONDS)
        if job is None:
            return
        await db_session.commit()
        try:
            source_id = UUID(str(job.payload["source_id"]))
            raw_page_token = job.payload.get("page_token")
            if raw_page_token is not None and not isinstance(raw_page_token, str):
                raise ValueError("invalid Drive sync page token")
            result = await DriveSyncService(
                db_session,
                connector_service=connector_service,
                drive_gateway_factory=gateway_factory,
            ).sync(source_id, raw_page_token)
            if result.reauth_required:
                await lease_service.retry(
                    job.id,
                    DRIVE_SYNC_WORKER_ID,
                    error_code="DRIVE_REAUTH_REQUIRED",
                    error_class=ErrorClass.NON_RETRYABLE,
                    expected_version=job.version,
                )
            else:
                await lease_service.complete(job.id, DRIVE_SYNC_WORKER_ID)
            await db_session.commit()
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception:
            await lease_service.retry(
                job.id,
                DRIVE_SYNC_WORKER_ID,
                error_code="DRIVE_SYNC_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
                expected_version=job.version,
            )
            await db_session.commit()
            raise
