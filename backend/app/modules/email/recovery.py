from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.connectors.models import Connector, ConnectorKind, ConnectorStatus
from app.modules.email.models import EmailSyncState, GmailSyncStatus
from app.modules.email.sync import GmailSyncCoordinator, GmailSyncRequest
from app.modules.identity.dependencies import Principal
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
        sync_request = await self._schedule(
            state, trigger="RECOVERY_CONTINUE", allow_recovery=True
        )
        await self._record(
            principal,
            state,
            "gmail.recovery.continue",
            reason=reason,
            choice="CONTINUE",
            additional_messages=additional_messages,
            sync_request=sync_request,
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
        sync_request = await self._schedule(
            state, trigger="RECOVERY_ACCEPT_LIMITED", allow_recovery=True
        )
        await self._record(
            principal,
            state,
            "gmail.recovery.accept_limited",
            reason=reason,
            choice="ACCEPT_LIMITED",
            additional_messages=0,
            sync_request=sync_request,
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
        sync_request = await self._schedule(
            state,
            trigger="MANUAL",
            requested_by=principal.subject_id,
        )
        await self._record(
            principal,
            state,
            "gmail.sync.manual",
            reason=reason,
            choice="MANUAL",
            additional_messages=0,
            sync_request=sync_request,
        )
        state.version += 1
        await self._db_session.flush()
        return state

    async def _locked_state(
        self, principal: Principal, connector_id: UUID, expected_version: int
    ) -> EmailSyncState:
        state = await self._db_session.scalar(
            select(EmailSyncState)
            .join(Connector, Connector.id == EmailSyncState.connector_id)
            .where(
                EmailSyncState.connector_id == connector_id,
                EmailSyncState.organization_id == principal.organization_id,
                Connector.organization_id == principal.organization_id,
                Connector.kind == ConnectorKind.GMAIL,
                Connector.status == ConnectorStatus.ACTIVE,
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

    async def _schedule(
        self,
        state: EmailSyncState,
        *,
        trigger: str,
        requested_by: UUID | None = None,
        allow_recovery: bool = False,
    ) -> GmailSyncRequest:
        knowledge_base_id = await self._db_session.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.organization_id == state.organization_id
            )
        )
        if knowledge_base_id is None:
            raise GmailRecoveryConflict("organization has no knowledge base")
        return await GmailSyncCoordinator(
            self._db_session,
            job_service=self._jobs,
            outbox_service=self._outbox,
        ).schedule_locked(
            state,
            knowledge_base_id=knowledge_base_id,
            trigger=trigger,
            requested_by=requested_by,
            allow_recovery=allow_recovery,
        )

    async def _record(
        self,
        principal: Principal,
        state: EmailSyncState,
        action: str,
        *,
        reason: str,
        choice: str,
        additional_messages: int,
        sync_request: GmailSyncRequest,
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
            "job_id": str(sync_request.job.id),
            "deduplicated": sync_request.deduplicated,
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
