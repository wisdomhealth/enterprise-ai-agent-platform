"""Executable Celery consumer for durable Drive synchronization intents."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import celery_async_sessionmaker as async_sessionmaker
from app.modules.connectors.service import ConnectorService
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService
from app.modules.knowledge.drive_gateway import GoogleDriveGatewayFactory
from app.modules.knowledge.ingestion import DocumentIngestionService
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    DriveSourceStatus,
)
from app.modules.knowledge.operations import enqueue_drive_sync_intent
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.knowledge.sync import DriveSyncService, SyncResult
from app.modules.outbox.models import OutboxEvent, ProcessedEvent
from app.modules.outbox.service import OutboxService

DRIVE_SYNC_TASK_NAME = "app.modules.knowledge.tasks.drive_source_sync"
DRIVE_SYNC_WORKER_ID = "celery-drive-sync"
DRIVE_SYNC_LEASE_SECONDS = 300
DRIVE_SYNC_RENEW_INTERVAL_SECONDS = 100
DOCUMENT_PARSE_TASK_NAME = "app.modules.knowledge.tasks.document_parse"
DOCUMENT_PARSE_WORKER_ID = "celery-document-parse"
DOCUMENT_PARSE_LEASE_SECONDS = 300
DOCUMENT_PARSE_REQUESTED_EVENT_TYPE = "knowledge.document.parse.requested"
DOCUMENT_CLEANUP_EVENT_TYPE = "knowledge.document.cleanup.requested"
DOCUMENT_CLEANUP_TASK_NAME = "app.modules.knowledge.tasks.document_cleanup"
DOCUMENT_CLEANUP_CONSUMER = "knowledge-document-cleanup-v1"


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


@shared_task(name=DOCUMENT_PARSE_TASK_NAME)  # type: ignore[untyped-decorator]
def document_parse(job_id: str) -> None:
    """Consume one durable document-parse intent through the authorized service."""
    asyncio.run(_consume_document_parse_intent(UUID(job_id)))


@shared_task(name="app.modules.knowledge.tasks.dispatch_drive_sync_outbox_event")  # type: ignore[untyped-decorator]
def dispatch_drive_sync_outbox_event(event_id: str) -> None:
    """At-least-once outbox delivery into the one Drive sync job consumer."""
    asyncio.run(_dispatch_drive_sync_outbox_event(UUID(event_id)))


@shared_task(name="app.modules.knowledge.tasks.dispatch_pending_drive_sync_outbox_events")  # type: ignore[untyped-decorator]
def dispatch_pending_drive_sync_outbox_events() -> None:
    """Retry durable events left behind by a post-commit broker wakeup failure."""
    asyncio.run(_dispatch_pending_drive_sync_outbox_events())


@shared_task(name="app.modules.knowledge.tasks.dispatch_document_parse_outbox_event")  # type: ignore[untyped-decorator]
def dispatch_document_parse_outbox_event(event_id: str) -> None:
    """At-least-once outbox delivery into one document-parse job consumer."""
    asyncio.run(_dispatch_document_parse_outbox_event(UUID(event_id)))


@shared_task(name="app.modules.knowledge.tasks.dispatch_pending_document_parse_outbox_events")  # type: ignore[untyped-decorator]
def dispatch_pending_document_parse_outbox_events() -> None:
    """Recover document-parse events left pending after broker wakeup loss."""
    asyncio.run(_dispatch_pending_document_parse_outbox_events())


@shared_task(  # type: ignore[untyped-decorator]
    name=DOCUMENT_CLEANUP_TASK_NAME,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=5,
)
def document_cleanup(event_id: str) -> None:
    """Idempotently delete chunks for the exact revoked versions in one event."""
    asyncio.run(_consume_document_cleanup_event(UUID(event_id)))


@shared_task(name="app.modules.knowledge.tasks.dispatch_document_cleanup_outbox_event")  # type: ignore[untyped-decorator]
def dispatch_document_cleanup_outbox_event(event_id: str) -> None:
    asyncio.run(_dispatch_document_cleanup_outbox_event(UUID(event_id)))


@shared_task(name="app.modules.knowledge.tasks.dispatch_pending_document_cleanup_outbox_events")  # type: ignore[untyped-decorator]
def dispatch_pending_document_cleanup_outbox_events() -> None:
    asyncio.run(_dispatch_pending_document_cleanup_outbox_events())


async def _dispatch_drive_sync_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_drive_sync_outbox_event(event_id, db_session=owned_session)
    event = await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or event.event_type != "knowledge.drive_source.sync.requested"
        or event.published_at is not None
    ):
        return False
    job_id = event.aggregate_id
    # Submit first. A broker failure leaves the row pending; a crash after a
    # successful submit can duplicate delivery, which the leased JobIntent
    # consumer safely absorbs.  Attempts are observable without turning a
    # failed delivery into a processed event.
    try:
        drive_source_sync.delay(str(job_id))
    except Exception:
        event.publish_attempts += 1
        await db_session.commit()
        raise
    event.publish_attempts += 1
    event.published_at = func.clock_timestamp()
    await db_session.commit()
    return True


async def _dispatch_pending_drive_sync_outbox_events(
    *, db_session: AsyncSession | None = None
) -> None:
    """Sweep durable events left pending after a process or broker failure."""
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_drive_sync_outbox_events(db_session=owned_session)
            return
    event_ids = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_id).where(
                    OutboxEvent.event_type == "knowledge.drive_source.sync.requested",
                    OutboxEvent.published_at.is_(None),
                )
            )
        ).all()
    )
    for event_id in event_ids:
        await _dispatch_drive_sync_outbox_event(event_id, db_session=db_session)


async def _dispatch_document_parse_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_document_parse_outbox_event(event_id, db_session=owned_session)
    event = await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or event.event_type != DOCUMENT_PARSE_REQUESTED_EVENT_TYPE
        or event.aggregate_type != "job"
        or event.published_at is not None
        or not await _parent_sync_succeeded(event, db_session)
    ):
        return False
    try:
        document_parse.delay(str(event.aggregate_id))
    except Exception:
        event.publish_attempts += 1
        await db_session.commit()
        raise
    event.publish_attempts += 1
    event.published_at = func.clock_timestamp()
    await db_session.commit()
    return True


async def _dispatch_pending_document_parse_outbox_events(
    *, db_session: AsyncSession | None = None
) -> None:
    """Sweep committed document-parse events that have no broker receipt."""
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_document_parse_outbox_events(db_session=owned_session)
            return
    event_ids = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_id).where(
                    OutboxEvent.event_type == DOCUMENT_PARSE_REQUESTED_EVENT_TYPE,
                    OutboxEvent.published_at.is_(None),
                )
            )
        ).all()
    )
    for event_id in event_ids:
        await _dispatch_document_parse_outbox_event(event_id, db_session=db_session)


async def _dispatch_document_cleanup_outbox_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _dispatch_document_cleanup_outbox_event(
                event_id, db_session=owned_session
            )
    event = await db_session.scalar(
        select(OutboxEvent)
        .where(OutboxEvent.event_id == event_id)
        .execution_options(populate_existing=True)
    )
    if (
        event is None
        or event.event_type != DOCUMENT_CLEANUP_EVENT_TYPE
        or event.aggregate_type != "document"
        or await db_session.get(
            ProcessedEvent,
            {
                "consumer_name": DOCUMENT_CLEANUP_CONSUMER,
                "event_id": event_id,
            },
        )
        is not None
    ):
        return False
    try:
        document_cleanup.delay(str(event.event_id))
    except Exception:
        event.publish_attempts += 1
        await db_session.commit()
        raise
    event.publish_attempts += 1
    if event.published_at is None:
        event.published_at = func.clock_timestamp()
    await db_session.commit()
    return True


async def _dispatch_pending_document_cleanup_outbox_events(
    *, db_session: AsyncSession | None = None
) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _dispatch_pending_document_cleanup_outbox_events(
                db_session=owned_session
            )
            return
    processed = select(ProcessedEvent.event_id).where(
        ProcessedEvent.consumer_name == DOCUMENT_CLEANUP_CONSUMER,
        ProcessedEvent.event_id == OutboxEvent.event_id,
    )
    event_ids = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_id).where(
                    OutboxEvent.event_type == DOCUMENT_CLEANUP_EVENT_TYPE,
                    OutboxEvent.aggregate_type == "document",
                    ~processed.exists(),
                )
            )
        ).all()
    )
    for event_id in event_ids:
        await _dispatch_document_cleanup_outbox_event(event_id, db_session=db_session)


async def _consume_document_cleanup_event(
    event_id: UUID, *, db_session: AsyncSession | None = None
) -> bool:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            return await _consume_document_cleanup_event(event_id, db_session=owned_session)
    try:
        event = await db_session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_id == event_id,
                OutboxEvent.event_type == DOCUMENT_CLEANUP_EVENT_TYPE,
                OutboxEvent.aggregate_type == "document",
            )
        )
        if event is None:
            await db_session.rollback()
            return False
        if not await OutboxService().begin_processing(
            db_session, DOCUMENT_CLEANUP_CONSUMER, event.event_id
        ):
            await db_session.rollback()
            return False

        organization_id = _cleanup_payload_uuid(event.payload, "organization_id")
        source_id = _cleanup_payload_uuid(event.payload, "source_id")
        document_id = _cleanup_payload_uuid(event.payload, "document_id")
        raw_version_ids = event.payload.get("version_ids")
        if (
            not isinstance(raw_version_ids, list)
            or not raw_version_ids
            or not all(isinstance(value, str) for value in raw_version_ids)
        ):
            raise ValueError("cleanup event requires version_ids")
        try:
            version_ids = [UUID(value) for value in raw_version_ids]
        except ValueError as exc:
            raise ValueError("cleanup event contains an invalid version ID") from exc
        if len(set(version_ids)) != len(version_ids):
            raise ValueError("cleanup event contains duplicate version IDs")
        if event.aggregate_id != document_id:
            raise ValueError("cleanup event aggregate mismatch")

        document = await db_session.scalar(
            select(Document)
            .join(DriveSource, DriveSource.id == Document.source_id)
            .where(
                Document.id == document_id,
                Document.organization_id == organization_id,
                Document.source_id == source_id,
                DriveSource.id == source_id,
                DriveSource.organization_id == organization_id,
            )
            .with_for_update()
        )
        if document is None:
            raise ValueError("cleanup event ownership mismatch")
        versions = list(
            (
                await db_session.scalars(
                    select(DocumentVersion)
                    .where(
                        DocumentVersion.id.in_(version_ids),
                        DocumentVersion.document_id == document.id,
                    )
                    .with_for_update()
                )
            ).all()
        )
        if len(versions) != len(version_ids):
            raise ValueError("cleanup event version ownership mismatch")
        if any(version.state is not DocumentVersionState.REVOKED for version in versions):
            raise ValueError("cleanup event may delete only revoked versions")

        await db_session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_version_id.in_(version_ids))
        )
        await db_session.commit()
        return True
    except Exception:
        await db_session.rollback()
        raise


def _cleanup_payload_uuid(payload: Mapping[str, object], field: str) -> UUID:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ValueError(f"cleanup event requires {field}")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"cleanup event contains an invalid {field}") from exc


async def _parent_sync_succeeded(event: OutboxEvent, db_session: AsyncSession) -> bool:
    binding = await _validated_parse_event_binding(event, db_session)
    if binding is None:
        return False
    _, source_id, _ = binding
    payload = event.payload
    if not isinstance(payload, Mapping):
        return False
    if "parent_sync_job_id" not in payload:
        return False
    raw_parent_job_id = payload["parent_sync_job_id"]
    if not isinstance(raw_parent_job_id, str):
        return False
    try:
        parent_job_id = UUID(raw_parent_job_id)
    except ValueError:
        return False
    return (
        await db_session.scalar(
            select(JobIntent.id).where(
                JobIntent.id == parent_job_id,
                JobIntent.kind == "knowledge.drive_source.sync",
                JobIntent.state == JobState.SUCCEEDED,
                JobIntent.payload["source_id"].as_string() == str(source_id),
                JobIntent.updated_at >= event.occurred_at,
            )
        )
    ) is not None


async def _validated_parse_event_binding(
    event: OutboxEvent, db_session: AsyncSession
) -> tuple[UUID, UUID, UUID] | None:
    payload = event.payload
    if not isinstance(payload, Mapping):
        return None
    raw_organization_id = payload.get("organization_id")
    raw_source_id = payload.get("source_id")
    raw_document_id = payload.get("document_id")
    if (
        not isinstance(raw_organization_id, str)
        or not isinstance(raw_source_id, str)
        or not isinstance(raw_document_id, str)
    ):
        return None
    try:
        organization_id = UUID(raw_organization_id)
        source_id = UUID(raw_source_id)
        document_id = UUID(raw_document_id)
    except ValueError:
        return None
    parse_job = await db_session.scalar(
        select(JobIntent).where(
            JobIntent.id == event.aggregate_id,
            JobIntent.kind == "knowledge.document.parse",
        )
    )
    if parse_job is None or not isinstance(parse_job.payload, Mapping):
        return None
    if (
        parse_job.payload.get("document_id") != raw_document_id
        or parse_job.payload.get("source_id") != raw_source_id
    ):
        return None
    document_exists = await db_session.scalar(
        select(Document.id)
        .join(DriveSource, DriveSource.id == Document.source_id)
        .where(
            Document.id == document_id,
            Document.organization_id == organization_id,
            Document.source_id == source_id,
            DriveSource.organization_id == organization_id,
        )
    )
    if document_exists is None:
        return None
    return organization_id, source_id, document_id


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


async def _renew_drive_sync_lease(
    job_id: UUID,
    execution_owner: str,
    expected_version: int,
) -> None:
    while True:
        await asyncio.sleep(DRIVE_SYNC_RENEW_INTERVAL_SECONDS)
        async with async_sessionmaker() as heartbeat_session:
            renewed = await JobLeaseService(heartbeat_session).renew(
                job_id,
                execution_owner,
                DRIVE_SYNC_LEASE_SECONDS,
                expected_version=expected_version,
            )
            await heartbeat_session.commit()
            expected_version = renewed.version


async def _run_drive_sync_with_lease_renewal(
    operation: Awaitable[SyncResult],
    *,
    job_id: UUID,
    execution_owner: str,
    expected_version: int,
) -> SyncResult:
    operation_task: asyncio.Future[SyncResult] = asyncio.ensure_future(operation)
    heartbeat_task: asyncio.Task[None] = asyncio.create_task(
        _renew_drive_sync_lease(job_id, execution_owner, expected_version)
    )
    waitables: set[asyncio.Future[Any]] = {operation_task, heartbeat_task}
    done, _pending = await asyncio.wait(
        waitables, return_when=asyncio.FIRST_COMPLETED
    )
    if heartbeat_task in done:
        operation_task.cancel()
        await asyncio.gather(operation_task, return_exceptions=True)
        await heartbeat_task
        raise JobLeaseLost(job_id)
    heartbeat_task.cancel()
    await asyncio.gather(heartbeat_task, return_exceptions=True)
    return await operation_task


async def _consume_drive_sync_intent(job_id: UUID) -> None:
    """The only consumer path for scheduled and manual sync intents."""
    async with async_sessionmaker() as db_session:
        lease_service = JobLeaseService(db_session)
        execution_owner = f"{DRIVE_SYNC_WORKER_ID}:{uuid4()}"
        job = await lease_service.claim(job_id, execution_owner, DRIVE_SYNC_LEASE_SECONDS)
        if job is None:
            return
        expected_version = job.version
        await db_session.commit()
        try:
            source_id = UUID(str(job.payload["source_id"]))
            source = await db_session.get(DriveSource, source_id)
            if source is None:
                raise LookupError("knowledge source not found")
            if source.status is not DriveSourceStatus.ACTIVE:
                # A source disabled for reauthorization (or by an operator) is
                # not a successful no-op.  Fence the terminal state through
                # the existing lease service before any credential lookup or
                # Drive I/O, so a newly enqueued manual request cannot create
                # false last-success evidence.
                error_code = (
                    "DRIVE_REAUTH_REQUIRED"
                    if source.status is DriveSourceStatus.ERROR
                    else "DRIVE_SOURCE_DISABLED"
                )
                await lease_service.retry(
                    job.id,
                    execution_owner,
                    error_code=error_code,
                    error_class=ErrorClass.NON_RETRYABLE,
                    expected_version=expected_version,
                )
                await db_session.commit()
                return
            settings = Settings()
            connector_service = ConnectorService.from_settings(settings)
            gateway_factory = GoogleDriveGatewayFactory.from_settings(settings)
            if connector_service is None or gateway_factory is None:
                raise RuntimeError("Google Drive connector credentials are not configured")
            raw_page_token = job.payload.get("page_token")
            if raw_page_token is not None and not isinstance(raw_page_token, str):
                raise ValueError("invalid Drive sync page token")
            result = await _run_drive_sync_with_lease_renewal(
                DriveSyncService(
                    db_session,
                    connector_service=connector_service,
                    drive_gateway_factory=gateway_factory,
                ).sync(
                    source_id,
                    raw_page_token,
                    parent_sync_job_id=job.id,
                ),
                job_id=job.id,
                execution_owner=execution_owner,
                expected_version=expected_version,
            )
            if result.reauth_required:
                await lease_service.retry(
                    job.id,
                    execution_owner,
                    error_code="DRIVE_REAUTH_REQUIRED",
                    error_class=ErrorClass.NON_RETRYABLE,
                    expected_version=expected_version,
                )
            else:
                await lease_service.complete(
                    job.id,
                    execution_owner,
                    expected_version=expected_version,
                )
            await db_session.commit()
            for event_id in result.parse_outbox_event_ids:
                try:
                    dispatch_document_parse_outbox_event.delay(str(event_id))
                except Exception:
                    # The committed event remains authoritative; the periodic
                    # sweep recovers this best-effort broker wakeup.
                    pass
            for event_id in result.cleanup_outbox_event_ids:
                try:
                    dispatch_document_cleanup_outbox_event.delay(str(event_id))
                except Exception:
                    # The cleanup event is already committed; the periodic
                    # dispatcher recovers a failed post-commit broker wakeup.
                    pass
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception:
            await lease_service.retry(
                job.id,
                execution_owner,
                error_code="DRIVE_SYNC_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
                expected_version=expected_version,
            )
            await db_session.commit()
            raise


async def _consume_document_parse_intent(job_id: UUID) -> None:
    """Run the existing lease-fenced authorized parsing pipeline for one intent."""
    async with async_sessionmaker() as db_session:
        settings = Settings()
        connector_service = ConnectorService.from_settings(settings)
        gateway_factory = GoogleDriveGatewayFactory.from_settings(settings)
        if connector_service is None or gateway_factory is None:
            lease_service = JobLeaseService(db_session)
            job = await lease_service.claim(
                job_id,
                DOCUMENT_PARSE_WORKER_ID,
                DOCUMENT_PARSE_LEASE_SECONDS,
            )
            if job is None:
                return
            await lease_service.retry(
                job.id,
                DOCUMENT_PARSE_WORKER_ID,
                error_code="DOCUMENT_PARSE_TRANSIENT_FAILURE",
                error_class=ErrorClass.RETRYABLE,
                expected_version=job.version,
            )
            await db_session.commit()
            raise RuntimeError("Google Drive connector credentials are not configured")
        service = DocumentIngestionService(
            db_session,
            knowledge_source_service=KnowledgeSourceService(
                connector_service,
                gateway_factory,
            ),
            worker_id=DOCUMENT_PARSE_WORKER_ID,
            job_lease_seconds=DOCUMENT_PARSE_LEASE_SECONDS,
        )
        await service.parse(job_id)
