from uuid import UUID

from app.modules.jobs.models import ErrorClass, JobIntent
from app.modules.jobs.service import JobLeaseService


class JobWorker:
    def __init__(self, lease_service: JobLeaseService) -> None:
        self._lease_service = lease_service

    async def handle_failure(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        error_code: str,
        error_class: ErrorClass,
        organization_id: UUID,
        actor_id: UUID,
        retry_after_seconds: int | None = None,
    ) -> JobIntent:
        return await self._retry(
            job_id,
            worker_id,
            error_code=error_code,
            error_class=error_class,
            retry_after_seconds=retry_after_seconds,
            organization_id=organization_id,
            actor_id=actor_id,
        )

    async def manual_retry(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        error_code: str,
        error_class: ErrorClass,
        organization_id: UUID,
        actor_id: UUID,
        retry_after_seconds: int | None = None,
    ) -> JobIntent:
        return await self._retry(
            job_id,
            worker_id,
            error_code=error_code,
            error_class=error_class,
            retry_after_seconds=retry_after_seconds,
            organization_id=organization_id,
            actor_id=actor_id,
        )

    async def _retry(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        error_code: str,
        error_class: ErrorClass,
        retry_after_seconds: int | None,
        organization_id: UUID,
        actor_id: UUID,
    ) -> JobIntent:
        return await self._lease_service.retry(
            job_id,
            worker_id,
            error_code=error_code,
            error_class=error_class,
            retry_after_seconds=retry_after_seconds,
            organization_id=organization_id,
            actor_id=actor_id,
        )
