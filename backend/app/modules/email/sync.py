import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email.models import EmailSyncState, GmailSyncStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.outbox.service import OutboxService

EMAIL_HISTORY_KIND = "email.gmail_history"
EMAIL_HISTORY_OUTBOX_EVENT_TYPE = "email.gmail_history.requested"
ACTIVE_JOB_STATES = {JobState.PENDING, JobState.RUNNING, JobState.RECONCILIATION}
BLOCKED_SYNC_STATES = {
    GmailSyncStatus.PAUSED_ERROR,
    GmailSyncStatus.RECOVERY_REQUIRED,
    GmailSyncStatus.RECOVERY_TRUNCATED,
}
logger = logging.getLogger(__name__)


class GmailSyncPaused(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GmailSyncRequest:
    job: JobIntent
    deduplicated: bool


class GmailSyncCoordinator:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        job_service: JobService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._jobs = job_service or JobService()
        self._outbox = outbox_service or OutboxService()

    async def schedule_locked(
        self,
        state: EmailSyncState,
        *,
        knowledge_base_id: UUID,
        trigger: str,
        requested_by: UUID | None = None,
        allow_recovery: bool = False,
    ) -> GmailSyncRequest:
        active = (
            await self._db_session.get(JobIntent, state.active_sync_job_id)
            if state.active_sync_job_id is not None
            else None
        )
        if active is not None and active.state in ACTIVE_JOB_STATES:
            self._log(state, active, trigger, requested_by, "DEDUPLICATED")
            return GmailSyncRequest(active, True)
        if state.sync_status in BLOCKED_SYNC_STATES and not allow_recovery:
            raise GmailSyncPaused(
                f"Gmail synchronization requires authorized recovery from {state.sync_status.value}"
            )
        state.active_sync_job_id = None
        state.sync_generation += 1
        state.sync_start_history_id = state.processed_history_id
        payload: dict[str, object] = {
            "connector_id": str(state.connector_id),
            "organization_id": str(state.organization_id),
            "knowledge_base_id": str(knowledge_base_id),
            "history_id": state.processed_history_id,
            "sync_generation": state.sync_generation,
            "trigger": trigger,
        }
        if requested_by is not None:
            payload["requested_by"] = str(requested_by)
        job = await self._jobs.enqueue(
            self._db_session,
            EMAIL_HISTORY_KIND,
            f"email.gmail_history:{state.connector_id}:generation:{state.sync_generation}",
            payload,
        )
        await self._outbox.add(
            self._db_session,
            EMAIL_HISTORY_OUTBOX_EVENT_TYPE,
            "job",
            job.id,
            {
                "job_id": str(job.id),
                "connector_id": str(state.connector_id),
                "organization_id": str(state.organization_id),
                "sync_generation": state.sync_generation,
                "trigger": trigger,
            },
        )
        state.active_sync_job_id = job.id
        if state.sync_status is not GmailSyncStatus.RECOVERY_RUNNING:
            state.sync_status = GmailSyncStatus.RUNNING
        self._log(state, job, trigger, requested_by, "ACCEPTED")
        return GmailSyncRequest(job, False)

    @staticmethod
    def _log(
        state: EmailSyncState,
        job: JobIntent,
        trigger: str,
        requested_by: UUID | None,
        outcome: str,
    ) -> None:
        logger.info(
            "Gmail synchronization request",
            extra={
                "organization_id": str(state.organization_id),
                "connector_id": str(state.connector_id),
                "job_id": str(job.id),
                "trigger": trigger,
                "requested_by": str(requested_by) if requested_by is not None else None,
                "outcome": outcome,
            },
        )
