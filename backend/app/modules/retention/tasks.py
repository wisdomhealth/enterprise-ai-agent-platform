"""Registered Celery consumers for durable retention and erasure work."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from celery import shared_task  # type: ignore[import-untyped]
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.outbox.service import OutboxService
from app.modules.retention.models import RetentionPolicy
from app.modules.retention.service import ErasureService, RetentionService

RETENTION_APPLY_KIND = "retention.apply_due"
ERASURE_APPLY_KIND = "retention.erasure.apply"
RETENTION_JOB_TASK_NAME = "app.modules.retention.tasks.retention_job"
RETENTION_WORKER_ID = "celery-retention"
RETENTION_LEASE_SECONDS = 300


@shared_task(name=RETENTION_JOB_TASK_NAME)  # type: ignore[untyped-decorator]
def retention_job(job_id: str) -> None:
    asyncio.run(_consume_retention_job(UUID(job_id)))


@shared_task(name="app.modules.retention.tasks.schedule_daily_retention")  # type: ignore[untyped-decorator]
def schedule_daily_retention() -> None:
    asyncio.run(_enqueue_daily_retention())


@shared_task(name="app.modules.retention.tasks.dispatch_pending_retention_jobs")  # type: ignore[untyped-decorator]
def dispatch_pending_retention_jobs() -> None:
    asyncio.run(_dispatch_pending_retention_jobs())


async def _enqueue_daily_retention(*, db_session: AsyncSession | None = None) -> None:
    if db_session is None:
        async with async_sessionmaker() as owned_session:
            await _enqueue_daily_retention(db_session=owned_session)
        return
    policies = list((await db_session.scalars(select(RetentionPolicy))).all())
    today = datetime.now(UTC).date().isoformat()
    job_ids: list[UUID] = []
    for policy in policies:
        job = await JobService().enqueue(
            db_session,
            RETENTION_APPLY_KIND,
            f"retention-daily:{policy.organization_id}:{today}",
            {"organization_id": str(policy.organization_id), "scheduled_date": today},
        )
        await OutboxService().add(
            db_session,
            "retention.apply_due.requested",
            "job",
            job.id,
            {"organization_id": str(policy.organization_id)},
        )
        job_ids.append(job.id)
    await db_session.commit()
    for job_id in job_ids:
        retention_job.delay(str(job_id))


async def _dispatch_pending_retention_jobs() -> None:
    async with async_sessionmaker() as db_session:
        job_ids = list(
            (
                await db_session.scalars(
                    select(JobIntent.id).where(
                        JobIntent.kind.in_((RETENTION_APPLY_KIND, ERASURE_APPLY_KIND)),
                        or_(
                            and_(
                                JobIntent.state == JobState.PENDING,
                                or_(
                                    JobIntent.next_attempt_at.is_(None),
                                    JobIntent.next_attempt_at <= func.clock_timestamp(),
                                ),
                            ),
                            and_(
                                JobIntent.state == JobState.RUNNING,
                                JobIntent.lease_expires_at.is_not(None),
                                JobIntent.lease_expires_at <= func.clock_timestamp(),
                            ),
                        ),
                    )
                )
            ).all()
        )
    for job_id in job_ids:
        retention_job.delay(str(job_id))


async def _consume_retention_job(job_id: UUID) -> None:
    async with async_sessionmaker() as db_session:
        lease_service = JobLeaseService(db_session)
        owner = f"{RETENTION_WORKER_ID}:{uuid4()}"
        job = await lease_service.claim(job_id, owner, RETENTION_LEASE_SECONDS)
        if job is None:
            return
        claimed_version = job.version
        await db_session.commit()
        try:
            if job.kind == RETENTION_APPLY_KIND:
                organization_id = UUID(str(job.payload["organization_id"]))
                database_now = await db_session.scalar(select(func.clock_timestamp()))
                if not isinstance(database_now, datetime):
                    raise RuntimeError("database clock unavailable")
                await RetentionService(db_session).apply_due(
                    organization_id, now=database_now, batch_size=500
                )
            elif job.kind == ERASURE_APPLY_KIND:
                request_id = UUID(str(job.payload["erasure_request_id"]))
                await ErasureService(db_session, hash_key=b"replay-does-not-use-key").apply(
                    request_id
                )
            else:
                raise ValueError("unsupported retention job kind")
            await lease_service.complete(job.id, owner, expected_version=claimed_version)
            await db_session.commit()
        except JobLeaseLost:
            await db_session.rollback()
            raise
        except Exception:
            await db_session.rollback()
            await lease_service.retry(
                job.id,
                owner,
                error_code="RETENTION_JOB_FAILED",
                error_class=ErrorClass.RETRYABLE,
                expected_version=claimed_version,
            )
            await db_session.commit()
            raise
