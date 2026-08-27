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
from app.modules.outbox.service import OutboxService
from app.modules.rag.types import AnswerAudience, ValidatedAnswer

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
    ) -> None:
        self._db_session = db_session
        self._answer_service = answer_service
        self._event_publisher = event_publisher
        self._worker_id = worker_id

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
                await lease_service.complete(
                    job.id, execution_owner, expected_version=job.version
                )
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
                select(ChatSession).where(ChatSession.id == session.id).with_for_update()
            )
            if (
                not isinstance(current_session, ChatSession)
                or current_session.state is not ConversationState.AI_ACTIVE
            ):
                await lease_service.complete(
                    job.id, execution_owner, expected_version=job.version
                )
                await self._db_session.commit()
                await self._notify(session.id, message.sequence)
                return None
            persisted = await self._persist_validated_answer(session, answer)
            # The message/outbox inserts and this generation-fenced compare
            # and set share one transaction.  PostgreSQL predicate
            # re-evaluation on the conditional UPDATE makes an expired or
            # taken-over lease roll the complete publication back.
            await lease_service.complete(
                job.id, execution_owner, expected_version=job.version
            )
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
                "citations": [citation.model_dump(mode="json") for citation in answer.citations],
                "refused": answer.refused,
                "handoff_recommended": answer.refused,
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
                },
            )
        await lease_service.retry(
            job.id,
            execution_owner,
            error_code=error_code,
            error_class=ErrorClass.NON_RETRYABLE,
            expected_version=job.version,
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
