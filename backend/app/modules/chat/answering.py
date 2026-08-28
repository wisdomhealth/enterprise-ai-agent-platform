"""Durable customer-chat answer processing.

The worker never streams provider tokens.  It first persists a complete,
validated customer-safe answer in PostgreSQL, commits it, then emits an
ephemeral Redis hint that causes connected SSE clients to reread PostgreSQL.
"""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import (
    ChatActor,
    ChatMessage,
    ChatMessageStatus,
    ChatSession,
    ConversationState,
)
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.jobs.models import ErrorClass, JobIntent
from app.modules.jobs.service import JobLeaseLost, JobLeaseService, JobService
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService
from app.modules.rag.types import AnswerAudience, CustomerCitation, ValidatedAnswer
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService
from app.modules.support.triggers import (
    NoSensitiveTopicClassifier,
    StructuredSafetyClassifier,
    choose_handoff_trigger,
)

CHAT_ANSWER_KIND = "chat.answer"
CHAT_ANSWER_WORKER_ID = "celery-chat-answer"
CHAT_ANSWER_LEASE_SECONDS = 300
_SAFE_ERROR = "I’m unable to answer that right now. A team member can help."


class CustomerAnswerService(Protocol):
    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer: ...


class ChatEventPublisher(Protocol):
    async def publish(self, session_id: UUID, sequence: int) -> None: ...


@dataclass(frozen=True, slots=True)
class SubmittedCustomerMessage:
    message: ChatMessage
    job: JobIntent


class ChatAnswerService:
    """Owns the persisted customer-message -> validated-answer job boundary."""

    def __init__(
        self,
        db_session: AsyncSession,
        answer_service: CustomerAnswerService,
        *,
        event_publisher: ChatEventPublisher | None = None,
        worker_id: str = CHAT_ANSWER_WORKER_ID,
        safety_classifier: StructuredSafetyClassifier | None = None,
    ) -> None:
        self._db_session = db_session
        self._answer_service = answer_service
        self._event_publisher = event_publisher
        self._worker_id = worker_id
        self._safety_classifier = safety_classifier or NoSensitiveTopicClassifier()

    async def submit_customer_message(
        self, session_id: UUID, body: str
    ) -> tuple[ChatMessage, JobIntent]:
        """Persist one customer message and its idempotent durable work intent."""
        session = await self._db_session.scalar(
            select(ChatSession).where(ChatSession.id == session_id).with_for_update()
        )
        if not isinstance(session, ChatSession):
            raise LookupError("chat session not found")
        sequence = await self._next_sequence(session.id)
        message = ChatMessage(
            session_id=session.id,
            sequence=sequence,
            actor=ChatActor.CUSTOMER,
            body=body,
            status=ChatMessageStatus.PERSISTED,
        )
        self._db_session.add(message)
        await self._db_session.flush()
        job = await JobService().enqueue(
            self._db_session,
            CHAT_ANSWER_KIND,
            f"chat.answer:{session.id}:{message.id}",
            {"session_id": str(session.id), "message_id": str(message.id)},
        )
        return message, job

    async def process(self, job_id: UUID) -> ChatMessage | None:
        """Claim one intent, persist its safe outcome, commit, then notify SSE."""
        lease_service = JobLeaseService(self._db_session)
        # A Celery hostname identifies a process, not a delivery.  The lease
        # owner has to fence this one execution so an expired delivery cannot
        # impersonate a later retry from the same consumer process.
        execution_owner = f"{self._worker_id}:{uuid4()}"
        job = await lease_service.claim(job_id, execution_owner, CHAT_ANSWER_LEASE_SECONDS)
        if job is None:
            return None
        # A durable claim must precede model I/O.  A crash after this point is
        # recoverable through the existing PostgreSQL lease state.
        await self._db_session.commit()
        try:
            session_id = UUID(str(job.payload["session_id"]))
            message_id = UUID(str(job.payload["message_id"]))
            session = await self._db_session.get(ChatSession, session_id)
            message = await self._db_session.get(ChatMessage, message_id)
            if (
                not isinstance(session, ChatSession)
                or not isinstance(message, ChatMessage)
                or message.session_id != session.id
                or message.actor is not ChatActor.CUSTOMER
            ):
                return await self._persist_safe_failure(
                    job, lease_service, execution_owner, session, "CHAT_JOB_INVALID"
                )
            if session.state is not ConversationState.AI_ACTIVE:
                await lease_service.complete(job.id, execution_owner, expected_version=job.version)
                await self._db_session.commit()
                await self._notify(session.id, message.sequence)
                return None

            try:
                classification = await self._safety_classifier.classify(message.body)
                sensitive_topic = classification.sensitive_topic
            except Exception:
                return await self._persist_safe_failure(
                    job,
                    lease_service,
                    execution_owner,
                    session,
                    "CHAT_SAFETY_CLASSIFIER_UNAVAILABLE",
                )
            if sensitive_topic is not None:
                await SupportService(self._db_session).request_handoff(
                    session.id,
                    trigger=HandoffTrigger.SENSITIVE_TOPIC,
                    sensitive_topic=sensitive_topic,
                )
                await lease_service.complete(job.id, execution_owner, expected_version=job.version)
                await self._db_session.commit()
                await self._notify(session.id, message.sequence)
                return None

            answer = await self._answer_service.answer(
                _public_session_principal(session),
                session.knowledge_base_id,
                message.body,
                AnswerAudience.CUSTOMER,
            )
            if not isinstance(answer, ValidatedAnswer):
                return await self._persist_safe_failure(
                    job,
                    lease_service,
                    execution_owner,
                    session,
                    "CHAT_ANSWER_UNVALIDATED",
                )
            # A handoff can start while generation is running.  Re-lock and
            # re-read durable state before publication so an old AI response
            # cannot appear after control leaves AI.
            current_session = await self._db_session.scalar(
                select(ChatSession)
                .where(ChatSession.id == session.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                not isinstance(current_session, ChatSession)
                or current_session.state is not ConversationState.AI_ACTIVE
            ):
                await lease_service.complete(job.id, execution_owner, expected_version=job.version)
                await self._db_session.commit()
                await self._notify(session.id, message.sequence)
                return None
            persisted = await self._persist_validated_answer(session, answer)
            trigger = await self._answer_handoff_trigger(session.id, persisted.id)
            if trigger is not None:
                await SupportService(self._db_session).request_handoff(session.id, trigger=trigger)
            # The message/outbox inserts and this generation-fenced compare
            # and set share one transaction.  PostgreSQL predicate
            # re-evaluation on the conditional UPDATE makes an expired or
            # taken-over lease roll the complete publication back.
            await lease_service.complete(job.id, execution_owner, expected_version=job.version)
            await self._db_session.commit()
            await self._notify(session.id, persisted.sequence)
            return persisted
        except JobLeaseLost:
            await self._db_session.rollback()
            raise
        except Exception:
            # Provider/validation details are deliberately not persisted in a
            # customer message or event.  The safe state recommends handoff.
            session = await self._session_for_job(job)
            return await self._persist_safe_failure(
                job,
                lease_service,
                execution_owner,
                session,
                "CHAT_ANSWER_UNAVAILABLE",
            )

    async def _persist_validated_answer(
        self, session: ChatSession, answer: ValidatedAnswer
    ) -> ChatMessage:
        sequence = await self._next_sequence(session.id)
        message = ChatMessage(
            session_id=session.id,
            sequence=sequence,
            actor=ChatActor.AI,
            body=answer.text,
            status=ChatMessageStatus.PERSISTED,
        )
        self._db_session.add(message)
        await self._db_session.flush()
        event_type = "chat.answer.refused" if answer.refused else "chat.answer.validated"
        await OutboxService().add(
            self._db_session,
            event_type,
            "chat_session",
            session.id,
            {
                "sequence": sequence,
                "message_id": str(message.id),
                "segments": list(answer.segments),
                # The outbox is the customer-visible evidence source for SSE.
                # Project here even if a caller accidentally supplies a staff
                # citation, so internal chunk IDs and Drive URLs never enter
                # the durable customer payload.
                "citations": [
                    CustomerCitation(
                        title=citation.title,
                        section=citation.section,
                        page_number=citation.page_number,
                    ).model_dump(mode="json")
                    for citation in answer.citations
                ],
                "refused": answer.refused,
                "supported_material_claims": sum(
                    1 for claim in answer.claims if claim.material
                ),
                "handoff_recommended": answer.refused,
                # Refusal text is visible as a single safe-error event, not
                # as a segment stream.  Persist its exact customer display
                # value in the same provenance record rather than deriving
                # it later from the mutable chat-message row.
                **({"safe_body": answer.text} if answer.refused else {}),
                "model": answer.model,
                "prompt_version": answer.prompt_version,
                "latency_ms": answer.latency_ms,
                "input_tokens": answer.input_tokens,
                "output_tokens": answer.output_tokens,
                "estimated_cost": answer.estimated_cost,
            },
        )
        return message

    async def _persist_safe_failure(
        self,
        job: JobIntent,
        lease_service: JobLeaseService,
        execution_owner: str,
        session: ChatSession | None,
        error_code: str,
    ) -> ChatMessage | None:
        message: ChatMessage | None = None
        if isinstance(session, ChatSession):
            current_session = await self._db_session.scalar(
                select(ChatSession)
                .where(ChatSession.id == session.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                not isinstance(current_session, ChatSession)
                or current_session.state is not ConversationState.AI_ACTIVE
            ):
                await lease_service.complete(
                    job.id,
                    execution_owner,
                    expected_version=job.version,
                )
                await self._db_session.commit()
                return None
            session = current_session
            sequence = await self._next_sequence(session.id)
            message = ChatMessage(
                session_id=session.id,
                sequence=sequence,
                actor=ChatActor.SYSTEM,
                body=_SAFE_ERROR,
                status=ChatMessageStatus.PERSISTED,
            )
            self._db_session.add(message)
            await self._db_session.flush()
            await OutboxService().add(
                self._db_session,
                "chat.answer.safe_error",
                "chat_session",
                session.id,
                {
                    "sequence": sequence,
                    "message_id": str(message.id),
                    "code": error_code,
                    "handoff_recommended": True,
                    # See the matching SSE provenance check: the customer
                    # display string must originate in this approved event.
                    "safe_body": _SAFE_ERROR,
                },
            )
        await lease_service.retry(
            job.id,
            execution_owner,
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
            expected_version=job.version,
        )
        # The safe error has already been persisted in the same transaction.
        # Queue the handoff before committing so a restart observes a complete,
        # explainable failure-to-human boundary rather than a transient hint.
        if isinstance(session, ChatSession):
            await SupportService(self._db_session).request_handoff(
                session.id, trigger=HandoffTrigger.SYSTEM_ERROR
            )
        await self._db_session.commit()
        if message is not None:
            await self._notify(message.session_id, message.sequence)
        return message

    async def _session_for_job(self, job: JobIntent) -> ChatSession | None:
        try:
            session_id = UUID(str(job.payload["session_id"]))
        except (KeyError, TypeError, ValueError):
            return None
        session = await self._db_session.get(ChatSession, session_id)
        return session if isinstance(session, ChatSession) else None

    async def _next_sequence(self, session_id: UUID) -> int:
        current = await self._db_session.scalar(
            select(func.coalesce(func.max(ChatMessage.sequence), 0)).where(
                ChatMessage.session_id == session_id
            )
        )
        return int(current or 0) + 1

    async def _answer_handoff_trigger(
        self, session_id: UUID, message_id: UUID
    ) -> HandoffTrigger | None:
        """Evaluate only chronological, message-bound durable answer provenance.

        Chat messages are not proof of an answer outcome: a worker can be
        interrupted after writing one, or a stale worker can leave a row behind.
        The matching Outbox event is the durable publication record.  We ignore
        every unbound, duplicate or malformed candidate so it cannot create a
        false repeated-failure escalation.
        """
        current_sequence = await self._db_session.scalar(
            select(ChatMessage.sequence).where(
                ChatMessage.id == message_id,
                ChatMessage.session_id == session_id,
            )
        )
        if not isinstance(current_sequence, int):
            return None
        messages = list(
            (
                await self._db_session.scalars(
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id == session_id,
                        ChatMessage.sequence <= current_sequence,
                        ChatMessage.actor.in_([ChatActor.AI, ChatActor.SYSTEM]),
                    )
                    .order_by(ChatMessage.sequence)
                )
            ).all()
        )
        events = list(
            (
                await self._db_session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_type == "chat_session",
                        OutboxEvent.aggregate_id == session_id,
                        OutboxEvent.event_type.in_(
                            [
                                "chat.answer.validated",
                                "chat.answer.refused",
                                "chat.answer.safe_error",
                            ]
                        ),
                    )
                )
            ).all()
        )
        events_by_message_id: dict[str, list[OutboxEvent]] = {}
        for event in events:
            raw_message_id = event.payload.get("message_id")
            if isinstance(raw_message_id, str):
                events_by_message_id.setdefault(raw_message_id, []).append(event)
        history = [
            turn
            for message in messages
            if (
                turn := _durable_answer_turn(
                    message, events_by_message_id.get(str(message.id), [])
                )
            )
            is not None
        ]
        return choose_handoff_trigger(history)

    async def _notify(self, session_id: UUID, sequence: int) -> None:
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher.publish(session_id, sequence)
        except Exception:
            # PostgreSQL remains authoritative; a later reconnect rereads it.
            return


def _public_session_principal(session: ChatSession) -> Principal:
    """A request-scoped principal bounded to the already-authorized session.

    The customer route has already checked its opaque bearer and fixed this
    principal to one organization/knowledge base.  The RAG path still applies
    its candidate authorization and eligibility predicates before generation.
    """
    return Principal(
        subject_id=session.id,
        organization_id=session.organization_id,
        email="public-chat@invalid.local",
        role=UserRole.MEMBER,
        session_id=session.id,
        csrf_hash="",
    )


def _durable_answer_turn(
    message: ChatMessage, candidates: list[OutboxEvent]
) -> dict[str, object] | None:
    """Project exactly one valid durable result into trigger history."""
    matches = [
        event
        for event in candidates
        if event.payload.get("message_id") == str(message.id)
        and event.payload.get("sequence") == message.sequence
    ]
    if len(matches) != 1:
        return None
    event = matches[0]
    payload = event.payload
    if message.actor is ChatActor.AI:
        if event.event_type == "chat.answer.refused" and payload.get("refused") is True:
            return {"refused": True, "supported_material_claims": 0}
        if event.event_type == "chat.answer.validated" and payload.get("refused") is False:
            supported_claims = payload.get("supported_material_claims", 1)
            if isinstance(supported_claims, int) and supported_claims >= 0:
                return {"refused": False, "supported_material_claims": supported_claims}
        return None
    if message.actor is ChatActor.SYSTEM:
        if (
            event.event_type == "chat.answer.safe_error"
            and isinstance(payload.get("code"), str)
            and payload.get("handoff_recommended") is True
            and payload.get("safe_body") == message.body
        ):
            return {"refused": True, "supported_material_claims": 0}
    return None
