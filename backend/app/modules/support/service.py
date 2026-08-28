from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.service import AuditService
from app.modules.authorization.policy import AuthorizationDenied, AuthorizationService
from app.modules.authorization.types import ResourceRef, ResourceState
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ConversationState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import ErrorClass, JobIntent, JobState
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService
from app.modules.support.models import Handoff, HandoffTrigger, SensitiveTopic, SupportAction
from app.modules.support.state_machine import InvalidTransition, transition


class SupportAuthorizationError(PermissionError):
    pass


class VersionConflict(RuntimeError):
    def __init__(self, *, state: ConversationState, version: int) -> None:
        super().__init__(f"handoff is {state} at version {version}")
        self.state = state
        self.version = version


ClaimedHandoff = Handoff


class SupportService:
    """Persisted handoff lifecycle. PostgreSQL is the ownership authority."""

    def __init__(
        self,
        db_session: AsyncSession,
        *,
        audit_service: AuditService | None = None,
        outbox_service: OutboxService | None = None,
    ) -> None:
        self._db_session = db_session
        self._audit_service = audit_service or AuditService()
        self._outbox_service = outbox_service or OutboxService()

    async def request_handoff(
        self,
        session_id: UUID,
        *,
        trigger: HandoffTrigger,
        sensitive_topic: SensitiveTopic | None = None,
    ) -> Handoff:
        session = await self._db_session.scalar(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
        if not isinstance(session, ChatSession):
            raise LookupError("chat session not found")
        existing = await self._db_session.scalar(
            select(Handoff)
            .where(
                Handoff.session_id == session.id,
                Handoff.state.in_(
                    [
                        ConversationState.HANDOFF_REQUESTED,
                        ConversationState.QUEUED,
                        ConversationState.HUMAN_ACTIVE,
                    ]
                ),
            )
            .order_by(Handoff.created_at.desc())
            .with_for_update()
        )
        if isinstance(existing, Handoff):
            return existing
        if session.state is not ConversationState.AI_ACTIVE:
            raise InvalidTransition(f"cannot request handoff from {session.state}")
        requested_state = transition(session.state, SupportAction.REQUEST_HANDOFF)
        assert requested_state is ConversationState.HANDOFF_REQUESTED
        snapshot, boundary = await self._snapshot(session)
        queued_state = transition(requested_state, SupportAction.QUEUE)
        assert queued_state is ConversationState.QUEUED
        handoff = Handoff(
            session_id=session.id,
            organization_id=session.organization_id,
            state=queued_state,
            trigger=trigger,
            sensitive_topic=sensitive_topic,
            snapshot=snapshot,
            last_customer_sequence=boundary,
        )
        self._db_session.add(handoff)
        session.state = queued_state
        session.version += 1
        await self._db_session.flush()
        await self._outbox_service.add(
            self._db_session,
            "support.handoff.queued",
            "chat_session",
            session.id,
            {
                "handoff_id": str(handoff.id),
                "trigger": trigger.value,
                "last_customer_sequence": boundary,
            },
        )
        return handoff

    async def claim(
        self, handoff_id: UUID, principal: Principal, expected_version: int
    ) -> ClaimedHandoff:
        self._require_staff(principal)
        await self._require_session_access(principal, handoff_id)
        claimed = await self._db_session.scalar(
            update(Handoff)
            .where(
                Handoff.id == handoff_id,
                Handoff.organization_id == principal.organization_id,
                Handoff.state == ConversationState.QUEUED,
                Handoff.version == expected_version,
            )
            .values(
                state=ConversationState.HUMAN_ACTIVE,
                assigned_user_id=principal.subject_id,
                version=Handoff.version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(Handoff)
        )
        if not isinstance(claimed, Handoff):
            await self._raise_conflict(handoff_id, principal.organization_id)
            raise AssertionError("unreachable after handoff conflict")
        updated_session = await self._db_session.scalar(
            update(ChatSession)
            .where(
                ChatSession.id == claimed.session_id, ChatSession.state == ConversationState.QUEUED
            )
            .values(
                state=ConversationState.HUMAN_ACTIVE,
                version=ChatSession.version + 1,
                updated_at=func.clock_timestamp(),
            )
            .returning(ChatSession.id)
        )
        if updated_session is None:
            raise RuntimeError("handoff/session state divergence")
        await self._audit_service.record(
            self._db_session,
            principal,
            action="support.handoff.claim",
            object_type="support_handoff",
            object_id=claimed.id,
            outcome="SUCCESS",
            details={"session_id": str(claimed.session_id)},
            safe_detail_keys={"session_id"},
        )
        await self._outbox_service.add(
            self._db_session,
            "support.handoff.claimed",
            "support_handoff",
            claimed.id,
            {"session_id": str(claimed.session_id), "assigned_user_id": str(principal.subject_id)},
        )
        await self._db_session.flush()
        return claimed

    async def reply(
        self, handoff_id: UUID, principal: Principal, expected_version: int, body: str
    ) -> ChatMessage:
        handoff = await self._owned_handoff(handoff_id, principal, expected_version)
        sequence = await self._next_sequence(handoff.session_id)
        message = ChatMessage(
            session_id=handoff.session_id, sequence=sequence, actor=ChatActor.STAFF, body=body
        )
        self._db_session.add(message)
        handoff.version += 1
        await self._audit_service.record(
            self._db_session,
            principal,
            action="support.handoff.reply",
            object_type="support_handoff",
            object_id=handoff.id,
            outcome="SUCCESS",
            details={"sequence": sequence},
            safe_detail_keys={"sequence"},
        )
        await self._outbox_service.add(
            self._db_session,
            "support.handoff.replied",
            "support_handoff",
            handoff.id,
            {"message_id": str(message.id), "sequence": sequence},
        )
        await self._db_session.flush()
        return message

    async def resolve(
        self, handoff_id: UUID, principal: Principal, expected_version: int
    ) -> Handoff:
        handoff = await self._owned_handoff(handoff_id, principal, expected_version)
        handoff.state = transition(handoff.state, SupportAction.RESOLVE) or handoff.state
        handoff.version += 1
        handoff.resolved_at = datetime.now(UTC)
        session = await self._db_session.get(ChatSession, handoff.session_id)
        if not isinstance(session, ChatSession):
            raise LookupError("chat session not found")
        session.state = ConversationState.RESOLVED
        session.version += 1
        await self._audit_service.record(
            self._db_session,
            principal,
            action="support.handoff.resolve",
            object_type="support_handoff",
            object_id=handoff.id,
            outcome="SUCCESS",
        )
        await self._outbox_service.add(
            self._db_session,
            "support.handoff.resolved",
            "support_handoff",
            handoff.id,
            {"session_id": str(session.id)},
        )
        await self._db_session.flush()
        return handoff

    async def resume_ai(
        self, handoff_id: UUID, principal: Principal, expected_version: int
    ) -> Handoff:
        handoff = await self._owned_handoff(handoff_id, principal, expected_version)
        handoff.state = transition(handoff.state, SupportAction.RESUME_AI) or handoff.state
        handoff.version += 1
        session = await self._db_session.get(ChatSession, handoff.session_id)
        if not isinstance(session, ChatSession):
            raise LookupError("chat session not found")
        session.state = ConversationState.AI_ACTIVE
        session.version += 1
        await self._clear_old_ai_jobs(session.id, handoff.last_customer_sequence)
        await self._audit_service.record(
            self._db_session,
            principal,
            action="support.handoff.resume_ai",
            object_type="support_handoff",
            object_id=handoff.id,
            outcome="SUCCESS",
            details={"boundary": handoff.last_customer_sequence},
            safe_detail_keys={"boundary"},
        )
        await self._outbox_service.add(
            self._db_session,
            "support.handoff.ai_resumed",
            "support_handoff",
            handoff.id,
            {
                "session_id": str(session.id),
                "handoff_boundary": handoff.last_customer_sequence,
                "await_customer_message": True,
            },
        )
        await self._db_session.flush()
        return handoff

    async def queue(self, principal: Principal) -> list[Handoff]:
        self._require_staff(principal)
        candidates = list(
            (
                await self._db_session.scalars(
                    select(Handoff)
                    .where(
                        Handoff.organization_id == principal.organization_id,
                        Handoff.state == ConversationState.QUEUED,
                    )
                    .order_by(Handoff.created_at)
                )
            ).all()
        )
        visible: list[Handoff] = []
        for handoff in candidates:
            try:
                await self._require_session_access(principal, handoff.id)
            except SupportAuthorizationError:
                continue
            visible.append(handoff)
        return visible

    async def _owned_handoff(
        self, handoff_id: UUID, principal: Principal, expected_version: int
    ) -> Handoff:
        self._require_staff(principal)
        handoff = await self._db_session.scalar(
            select(Handoff)
            .where(Handoff.id == handoff_id, Handoff.organization_id == principal.organization_id)
            .with_for_update()
        )
        if not isinstance(handoff, Handoff):
            raise LookupError("handoff not found")
        await self._require_session_access(principal, handoff.id)
        if (
            handoff.state is not ConversationState.HUMAN_ACTIVE
            or handoff.version != expected_version
        ):
            raise VersionConflict(state=handoff.state, version=handoff.version)
        if (
            principal.role is not UserRole.ADMIN
            and handoff.assigned_user_id != principal.subject_id
        ):
            raise SupportAuthorizationError
        return handoff

    async def _raise_conflict(self, handoff_id: UUID, organization_id: UUID) -> None:
        current = await self._db_session.scalar(
            select(Handoff).where(
                Handoff.id == handoff_id, Handoff.organization_id == organization_id
            )
        )
        if isinstance(current, Handoff):
            raise VersionConflict(state=current.state, version=current.version)
        raise LookupError("handoff not found")

    @staticmethod
    def _require_staff(principal: Principal) -> None:
        if principal.role not in {UserRole.ADMIN, UserRole.REVIEWER}:
            raise SupportAuthorizationError

    async def _require_session_access(self, principal: Principal, handoff_id: UUID) -> None:
        session = await self._db_session.scalar(
            select(ChatSession)
            .join(Handoff, Handoff.session_id == ChatSession.id)
            .where(Handoff.id == handoff_id)
        )
        if not isinstance(session, ChatSession):
            raise LookupError("handoff not found")
        try:
            await AuthorizationService(self._db_session).require(
                principal,
                "knowledge.review",
                ResourceRef(
                    organization_id=session.organization_id,
                    resource_type="knowledge",
                    resource_id=session.knowledge_base_id,
                    state=ResourceState.ACTIVE,
                ),
            )
        except AuthorizationDenied as error:
            raise SupportAuthorizationError from error

    async def _snapshot(self, session: ChatSession) -> tuple[dict[str, object], int]:
        messages = list(
            (
                await self._db_session.scalars(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == session.id)
                    .order_by(ChatMessage.sequence)
                )
            ).all()
        )
        events = list(
            (
                await self._db_session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == session.id,
                        OutboxEvent.aggregate_type == "chat_session",
                    )
                )
            ).all()
        )
        customer_messages = [message for message in messages if message.actor is ChatActor.CUSTOMER]
        citations = [
            event.payload.get("citations", [])
            for event in events
            if event.event_type in {"chat.answer.validated", "chat.answer.refused"}
        ]
        boundary = max((message.sequence for message in customer_messages), default=0)
        return (
            {
                "transcript": [
                    {
                        "message_id": str(message.id),
                        "sequence": message.sequence,
                        "actor": message.actor.value,
                    }
                    for message in messages
                ],
                "summary": "",
                "customer": {"name": session.customer_name, "email": session.customer_email},
                "citations": citations,
                "tool_results": [],
                "last_customer_sequence": boundary,
            },
            boundary,
        )

    async def _clear_old_ai_jobs(self, session_id: UUID, boundary: int) -> None:
        jobs = list(
            (
                await self._db_session.scalars(
                    select(JobIntent).where(
                        JobIntent.kind == "chat.answer",
                        JobIntent.state.in_([JobState.PENDING, JobState.RUNNING]),
                    )
                )
            ).all()
        )
        message_ids = {
            str(message.id)
            for message in (
                await self._db_session.scalars(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.sequence <= boundary,
                        ChatMessage.actor == ChatActor.CUSTOMER,
                    )
                )
            ).all()
        }
        for job in jobs:
            if (
                str(job.payload.get("session_id")) == str(session_id)
                and str(job.payload.get("message_id")) in message_ids
            ):
                job.state = JobState.FAILED
                job.lease_owner = None
                job.lease_expires_at = None
                job.next_attempt_at = None
                job.last_error_code = "HANDOFF_RESUME_STALE"
                job.error_class = ErrorClass.NON_RETRYABLE
                job.version += 1

    async def _next_sequence(self, session_id: UUID) -> int:
        value = await self._db_session.scalar(
            select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
                ChatMessage.session_id == session_id
            )
        )
        return int(value or 0) + 1
