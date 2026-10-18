from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.email.models import EmailSyncState, GmailSyncStatus
from app.modules.email.tasks import EMAIL_HISTORY_KIND, EMAIL_HISTORY_OUTBOX_EVENT_TYPE
from app.modules.identity.dependencies import Principal
from app.modules.jobs.models import JobIntent, JobState
from app.modules.jobs.service import JobService
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.service import OutboxService


class GmailRecoveryConflict(ValueError):
    pass


class GmailRecoveryService:
    def __init__(
        self,
        db_session: AsyncSession,
        *,
        job_service: JobService | None = None,
        outbox_service: OutboxService | None = None,
        audit_service: AuditService | None = None,
    ) -> None:
        self._db_session = db_session
        self._jobs = job_service or JobService()
        self._outbox = outbox_service or OutboxService()
        self._audit = audit_service or AuditService()

    async def continue_recovery(
        self,
        principal: Principal,
        connector_id: UUID,
        *,
        expected_version: int,
        additional_messages: int,
        reason: str,
    ) -> EmailSyncState:
        if additional_messages < 1 or not reason.strip():
            raise ValueError("additional_messages and reason are required")
        state = await self._locked_state(principal, connector_id, expected_version)
        self._require_truncated(state)
        state.recovery_message_limit += additional_messages
        state.sync_status = GmailSyncStatus.RECOVERY_RUNNING
        await self._schedule(state, trigger="RECOVERY_CONTINUE")
        await self._record(
            principal,
            state,
            "gmail.recovery.continue",
            reason=reason,
            choice="CONTINUE",
            additional_messages=additional_messages,
        )
        state.version += 1
        await self._db_session.flush()
        return state

    async def accept_limited_recovery(
        self,
        principal: Principal,
        connector_id: UUID,
        *,
        expected_version: int,
        confirmed: bool,
        reason: str,
    ) -> EmailSyncState:
        if not confirmed or not reason.strip():
            raise ValueError("explicit confirmation and reason are required")
        state = await self._locked_state(principal, connector_id, expected_version)
        self._require_truncated(state)
        state.recovery_has_more = False
        state.recovery_page_token = None
        state.sync_status = GmailSyncStatus.RECOVERY_RUNNING
        await self._schedule(state, trigger="RECOVERY_ACCEPT_LIMITED")
        await self._record(
            principal,
            state,
            "gmail.recovery.accept_limited",
            reason=reason,
            choice="ACCEPT_LIMITED",
            additional_messages=0,
        )
        state.version += 1
        await self._db_session.flush()
        return state

    async def manual_sync(
        self,
        principal: Principal,
        connector_id: UUID,
        *,
        expected_version: int,
        reason: str,
    ) -> EmailSyncState:
        if not reason.strip():
            raise ValueError("reason is required")
        state = await self._locked_state(principal, connector_id, expected_version)
        if state.sync_status is GmailSyncStatus.RECOVERY_TRUNCATED:
            raise GmailRecoveryConflict("truncated recovery requires continue or accept")
        await self._schedule(state, trigger="MANUAL")
        await self._record(
            principal,
            state,
            "gmail.sync.manual",
            reason=reason,
            choice="MANUAL",
            additional_messages=0,
        )
        state.version += 1
        await self._db_session.flush()
        return state

    async def _locked_state(
        self, principal: Principal, connector_id: UUID, expected_version: int
    ) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .where(
                EmailSyncState.connector_id == connector_id,
                EmailSyncState.organization_id == principal.organization_id,
            )
            .with_for_update()
        )
        if state is None:
            raise LookupError("Gmail sync state not found")
        if state.version != expected_version:
            raise GmailRecoveryConflict("Gmail sync state version conflict")
        return state

    @staticmethod
    def _require_truncated(state: EmailSyncState) -> None:
        if (
            state.sync_status is not GmailSyncStatus.RECOVERY_TRUNCATED
            or state.recovery_batch_id is None
            or state.recovery_baseline_history_id is None
        ):
            raise GmailRecoveryConflict("Gmail recovery is not truncated")

    async def _schedule(self, state: EmailSyncState, *, trigger: str) -> None:
        active = (
            await self._db_session.get(JobIntent, state.active_sync_job_id)
            if state.active_sync_job_id is not None
            else None
        )
        if active is not None and active.state in {
            JobState.PENDING,
            JobState.RUNNING,
            JobState.RECONCILIATION,
        }:
            raise GmailRecoveryConflict("Gmail synchronization is already active")
        knowledge_base_id = await self._db_session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.organization_id == state.organization_id
            )
        )
        if knowledge_base_id is None:
            raise GmailRecoveryConflict("organization has no knowledge base")
        state.sync_generation += 1
        job = await self._jobs.enqueue(
            self._db_session,
            EMAIL_HISTORY_KIND,
            f"email.gmail_history:{state.connector_id}:generation:{state.sync_generation}",
            {
                "connector_id": str(state.connector_id),
                "organization_id": str(state.organization_id),
                "knowledge_base_id": str(knowledge_base_id),
                "history_id": state.processed_history_id,
                "sync_generation": state.sync_generation,
                "trigger": trigger,
            },
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

    async def _record(
        self,
        principal: Principal,
        state: EmailSyncState,
        action: str,
        *,
        reason: str,
        choice: str,
        additional_messages: int,
    ) -> None:
        details = {
            "connector_id": str(state.connector_id),
            "recovery_batch_id": str(state.recovery_batch_id),
            "recovery_window_started_at": (
                state.recovery_window_started_at.isoformat()
                if state.recovery_window_started_at is not None
                else None
            ),
            "recovery_processed_count": state.recovery_processed_count,
            "recovery_message_limit": state.recovery_message_limit,
            "recovery_has_more": state.recovery_has_more,
            "choice": choice,
            "additional_messages": additional_messages,
            "reason": reason.strip(),
        }
        await self._audit.record(
            self._db_session,
            principal,
            action=action,
            object_type="email_sync_state",
            object_id=state.id,
            outcome="SUCCESS",
            details=details,
            safe_detail_keys=set(details),
        )
