"""Staff-triggered sync operations and safe operability status."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.knowledge.models import (
    Document,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
)
from app.modules.knowledge.service import KnowledgeSourceService
from app.modules.knowledge.sync import drive_sync_job_key
from app.modules.outbox.service import OutboxService


@dataclass(frozen=True, slots=True)
class DriveSyncStatus:
    source_id: UUID
    cursor: str | None
    source_status: str
    last_success_at: datetime | None
    backlog: int
    isolated_files: int
    retry_count: int
    recent_error_codes: list[str]


@dataclass(frozen=True, slots=True)
class EnqueuedDriveSync:
    job: JobIntent
    outbox_event_id: UUID | None


class DriveSyncOperations:
    def __init__(
        self,
        db_session: AsyncSession,
        knowledge_source_service: KnowledgeSourceService,
        *,
        job_service: JobService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._knowledge_source_service = knowledge_source_service
        self._job_service = job_service or JobService()
        self._outbox_service = outbox_service or OutboxService()

    async def enqueue_sync(self, *, principal: Principal, source_id: UUID) -> JobIntent:
        return (await self.enqueue_sync_for_dispatch(principal=principal, source_id=source_id)).job

    async def enqueue_sync_for_dispatch(
        self, *, principal: Principal, source_id: UUID
    ) -> EnqueuedDriveSync:
        if principal.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        await self._require_configuration_authorization(principal)
        source = await self._db_session.scalar(
            select(DriveSource).where(
                DriveSource.id == source_id,
                DriveSource.organization_id == principal.organization_id,
            )
        )
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return await enqueue_drive_sync_intent(
            self._db_session,
            source,
            job_service=self._job_service,
            outbox_service=self._outbox_service,
        )

    async def status(self, *, principal: Principal, source_id: UUID) -> DriveSyncStatus:
        if principal.role is not UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        await self._require_configuration_authorization(principal)
        source = await self._db_session.scalar(
            select(DriveSource).where(
                DriveSource.id == source_id,
                DriveSource.organization_id == principal.organization_id,
            )
        )
        if source is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        jobs = list(
            (
                await self._db_session.scalars(
                    select(JobIntent).where(JobIntent.kind == "knowledge.drive_source.sync")
                )
            ).all()
        )
        source_jobs = [job for job in jobs if job.payload.get("source_id") == str(source.id)]
        documents = list(
            (
                await self._db_session.scalars(
                    select(Document).where(Document.source_id == source.id)
                )
            ).all()
        )
        document_ids = [document.id for document in documents]
        isolated = 0
        if document_ids:
            isolated = int(
                await self._db_session.scalar(
                    select(func.count(DocumentVersion.id)).where(
                        DocumentVersion.document_id.in_(document_ids),
                        DocumentVersion.state == DocumentVersionState.REVOKED,
                    )
                )
                or 0
            )
        successful_jobs = [job for job in source_jobs if job.state is JobState.SUCCEEDED]
        last_success = max(
            (job.updated_at for job in successful_jobs),
            default=None,
        )
        errors = [job.last_error_code for job in source_jobs if job.last_error_code][-5:]
        return DriveSyncStatus(
            source_id=source.id,
            cursor=source.sync_cursor,
            source_status=source.status.value,
            last_success_at=last_success,
            backlog=sum(job.state in (JobState.PENDING, JobState.RUNNING) for job in source_jobs),
            isolated_files=isolated,
            retry_count=sum(job.attempts for job in source_jobs),
            recent_error_codes=[error for error in errors if error is not None],
        )

    async def _require_configuration_authorization(self, principal: Principal) -> None:
        resource = ResourceRef(
            organization_id=principal.organization_id,
            resource_type="knowledge",
            resource_id=self._knowledge_source_service.configuration_resource_id(
                principal.organization_id
            ),
            state=ResourceState.ACTIVE,
        )
        try:
            await AuthorizationService(self._db_session).require(
                principal, "knowledge.write", resource
            )
        except AuthorizationDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN) from exc


async def enqueue_drive_sync_intent(
    db_session: AsyncSession,
    source: DriveSource,
    *,
    job_service: JobService | None = None,
    outbox_service: OutboxService | None = None,
) -> EnqueuedDriveSync:
    """One durable intent producer shared by staff and periodic scheduling."""
    job_service = job_service or JobService()
    outbox_service = outbox_service or OutboxService()
    base_key = drive_sync_job_key(source.id, source.sync_cursor)
    existing = await db_session.scalar(
        select(JobIntent)
        .where(
            JobIntent.kind == "knowledge.drive_source.sync",
            (JobIntent.idempotency_key == base_key)
            | JobIntent.idempotency_key.like(f"{base_key}:run:%"),
        )
        .order_by(JobIntent.created_at.desc())
        .limit(1)
    )
    if existing is not None and existing.state in (
        JobState.PENDING,
        JobState.RUNNING,
        JobState.RECONCILIATION,
    ):
        return EnqueuedDriveSync(job=existing, outbox_event_id=None)
    idempotency_key = (
        base_key
        if existing is None
        else f"{base_key}:run:{existing.version}"
    )
    job = await job_service.enqueue(
        db_session,
        "knowledge.drive_source.sync",
        idempotency_key,
        {"source_id": str(source.id), "page_token": source.sync_cursor},
    )
    outbox_event = await outbox_service.add(
        db_session,
        "knowledge.drive_source.sync.requested",
        "job",
        job.id,
        {"source_id": str(source.id)},
    )
    return EnqueuedDriveSync(job=job, outbox_event_id=outbox_event.event_id)
