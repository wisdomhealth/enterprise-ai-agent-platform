from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ConversationState
from app.modules.identity.models import Organization
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.types import ValidatedAnswer


class UnvalidatedAnswerService:
    async def answer(self, *_args: object, **_kwargs: object) -> object:
        return object()


class CountingAnswerService:
    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        self.calls += 1
        raise AssertionError("the model must not be called for a human-owned session")


class HandoffDuringAnswerService:
    def __init__(self, db_session: AsyncSession, session: ChatSession) -> None:
        self._db_session = db_session
        self._session = session

    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        self._session.state = ConversationState.HANDOFF_REQUESTED
        await self._db_session.flush()
        return ValidatedAnswer(
            text="This answer must not be published after handoff.",
            claims=[],
            citations=[],
            segments=["This answer must not be published after handoff."],
            refused=False,
            model="fake",
            prompt_version="test",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.0,
        )


async def _session(db_session: AsyncSession) -> ChatSession:
    organization = Organization(name=f"Chat answer {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{uuid4().hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
    )
    db_session.add(session)
    await db_session.flush()
    return session


@pytest.mark.asyncio
async def test_no_sse_segment_exists_before_answer_validation(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    chat_service = ChatAnswerService(db_session, UnvalidatedAnswerService())
    _, job = await chat_service.submit_customer_message(session.id, "What is the policy?")
    await db_session.commit()

    await chat_service.process(job.id)

    event_types = list(
        (
            await db_session.scalars(
                select(OutboxEvent.event_type).where(OutboxEvent.aggregate_id == session.id)
            )
        ).all()
    )
    messages = list(
        (
            await db_session.scalars(
                select(ChatMessage).where(ChatMessage.session_id == session.id)
            )
        ).all()
    )
    assert "chat.message.segment" not in event_types
    assert [message.actor for message in messages] == [ChatActor.CUSTOMER, ChatActor.SYSTEM]
    assert "team member" in messages[-1].body


@pytest.mark.asyncio
async def test_worker_does_not_call_model_when_ai_is_not_active(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    session.state = ConversationState.HUMAN_ACTIVE
    fake_answer_service = CountingAnswerService()
    chat_service = ChatAnswerService(db_session, fake_answer_service)
    _, job = await chat_service.submit_customer_message(session.id, "Can you help?")
    await db_session.commit()

    await chat_service.process(job.id)

    assert fake_answer_service.calls == 0
    job_state = await db_session.scalar(select(JobIntent.state).where(JobIntent.id == job.id))
    assert job_state is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_worker_does_not_publish_answer_after_handoff_begins(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    chat_service = ChatAnswerService(
        db_session, HandoffDuringAnswerService(db_session, session)
    )
    _, job = await chat_service.submit_customer_message(session.id, "Can you help?")
    await db_session.commit()

    await chat_service.process(job.id)

    ai_count = await db_session.scalar(
        select(func.count()).select_from(ChatMessage).where(
            ChatMessage.session_id == session.id,
            ChatMessage.actor == ChatActor.AI,
        )
    )
    assert ai_count == 0
