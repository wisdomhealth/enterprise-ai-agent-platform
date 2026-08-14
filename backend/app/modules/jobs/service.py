from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from random import random
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.jobs.models import ErrorClass, JobIntent, JobState


class JobLeaseLost(Exception):
    pass


class RetryPolicy:
    def __init__(
        self,
        *,
        base_seconds: int = 5,
        max_seconds: int = 300,
        jitter: Callable[[], float] = random,
    ) -> None:
        if base_seconds <= 0 or max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        self._base_seconds = base_seconds
        self._max_seconds = max_seconds
        self._jitter = jitter

    def delay_seconds(
        self,
        *,
        attempts: int,
        retry_after_seconds: int | None = None,
    ) -> int:
        attempt_number = max(1, attempts)
        exponential = self._base_seconds * (2 ** (attempt_number - 1))
        jittered = int(exponential * (1 + max(0.0, self._jitter())))
        bounded = min(self._max_seconds, jittered)
        if retry_after_seconds is None:
            return bounded
        return max(bounded, max(0, retry_after_seconds))


class JobService:
    async def enqueue(
        self,
        db_session: AsyncSession,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, object],
    ) -> JobIntent:
        job_id = await db_session.scalar(
            insert(JobIntent)
            .values(kind=kind, idempotency_key=idempotency_key, payload=dict(payload))
            .on_conflict_do_nothing(
                index_elements=[JobIntent.kind, JobIntent.idempotency_key]
            )
            .returning(JobIntent.id)
        )
        job = await db_session.scalar(
            select(JobIntent).where(
                JobIntent.id == job_id
                if job_id is not None
                else and_(
                    JobIntent.kind == kind,
                    JobIntent.idempotency_key == idempotency_key,
                )
            )
        )
        if job is None:
            raise RuntimeError("job enqueue did not return the inserted or existing job")
        return job


class JobLeaseService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        retry_policy: RetryPolicy | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._db_session = db_session
        self._retry_policy = retry_policy or RetryPolicy()
        self._audit_service = audit_service or AuditService()

    async def claim(
        self,
        job_id: UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> JobIntent | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        claimable = or_(
            and_(
                JobIntent.state == JobState.PENDING,
                or_(JobIntent.next_attempt_at.is_(None), JobIntent.next_attempt_at <= now),
            ),
            and_(
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_expires_at.is_not(None),
                JobIntent.lease_expires_at <= now,
            ),
        )
        return await self._db_session.scalar(
            update(JobIntent)
            .where(JobIntent.id == job_id, claimable)
            .values(
                state=JobState.RUNNING,
                lease_owner=worker_id,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=JobIntent.attempts + 1,
                next_attempt_at=None,
                version=JobIntent.version + 1,
                updated_at=now,
            )
            .returning(JobIntent)
        )

    async def complete(self, job_id: UUID, worker_id: str) -> JobIntent:
        now = datetime.now(UTC)
        completed = await self._db_session.scalar(
            update(JobIntent)
            .where(
                JobIntent.id == job_id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == worker_id,
                JobIntent.lease_expires_at > now,
            )
            .values(
                state=JobState.SUCCEEDED,
                lease_owner=None,
                lease_expires_at=None,
                next_attempt_at=None,
                version=JobIntent.version + 1,
                updated_at=now,
            )
            .returning(JobIntent)
        )
        if completed is not None:
            return completed
        existing = await self._db_session.get(JobIntent, job_id)
        if existing is not None and existing.state is JobState.SUCCEEDED:
            return existing
        raise JobLeaseLost(job_id)

    async def retry(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        error_code: str,
        error_class: ErrorClass,
        retry_after_seconds: int | None = None,
        organization_id: UUID | None = None,
        actor_id: UUID | None = None,
    ) -> JobIntent:
        if error_class is ErrorClass.SECURITY and (
            organization_id is None or actor_id is None
        ):
            raise ValueError("security job failures require organization_id and actor_id")
        now = datetime.now(UTC)
        job = await self._db_session.scalar(
            select(JobIntent)
            .where(
                JobIntent.id == job_id,
                JobIntent.state == JobState.RUNNING,
                JobIntent.lease_owner == worker_id,
                JobIntent.lease_expires_at > now,
            )
            .with_for_update()
        )
        if job is None:
            raise JobLeaseLost(job_id)

        next_attempt_at: datetime | None = None
        if error_class is ErrorClass.RETRYABLE:
            state = JobState.PENDING
            delay = self._retry_policy.delay_seconds(
                attempts=job.attempts,
                retry_after_seconds=retry_after_seconds,
            )
            next_attempt_at = now + timedelta(seconds=delay)
        elif error_class is ErrorClass.AMBIGUOUS:
            state = JobState.RECONCILIATION
        else:
            state = JobState.FAILED

        job.state = state
        job.lease_owner = None
        job.lease_expires_at = None
        job.next_attempt_at = next_attempt_at
        job.last_error_code = error_code
        job.error_class = error_class
        job.version += 1
        job.updated_at = now

        if error_class is ErrorClass.SECURITY:
            assert organization_id is not None
            assert actor_id is not None
            await self._audit_service.record_actor(
                self._db_session,
                organization_id=organization_id,
                actor_id=actor_id,
                action="job.security_denied",
                object_type="job",
                object_id=job.id,
                outcome="DENIED",
                details={"error_code": error_code},
                safe_detail_keys={"error_code"},
            )
        await self._db_session.flush()
        return job

    async def fail_terminal(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        error_code: str,
    ) -> JobIntent:
        return await self.retry(
            job_id,
            worker_id,
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
        )
