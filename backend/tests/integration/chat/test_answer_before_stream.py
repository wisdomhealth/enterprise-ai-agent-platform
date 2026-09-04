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
from app.modules.rag.answer_service import AnswerExecution
from app.modules.rag.types import CustomerCitation, SourceCitation, ValidatedAnswer


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


class RefusalWithStaffCitationService:
    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        return ValidatedAnswer(
            text="I don't know based on the available information.",
            claims=[],
            citations=[
                SourceCitation(
                    chunk_id=uuid4(),
                    document_version_id=uuid4(),
                    title="Customer-safe title",
                    section="Overview",
                    page_number=4,
                    internal_drive_link="https://drive.example.internal/private",
                )
            ],
            segments=["I don't know based on the available information."],
            refused=True,
            model="fake",
            prompt_version="test",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.0,
        )


class ValidatedEvidenceAnswerService:
    def __init__(self) -> None:
        self.source = SourceCitation(
            chunk_id=uuid4(),
            document_version_id=uuid4(),
            title="Customer-safe title",
            section="Overview",
            page_number=4,
            internal_drive_link="https://drive.example.internal/private",
        )

    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        raise AssertionError("chat generation must use the evidence-preserving boundary")

    async def answer_with_evidence(
        self, *_args: object, **_kwargs: object
    ) -> AnswerExecution:
        return AnswerExecution(
            answer=ValidatedAnswer(
                text="The validated answer.",
                claims=[],
                citations=[
                    CustomerCitation(
                        title=self.source.title,
                        section=self.source.section,
                        page_number=self.source.page_number,
                    )
                ],
                segments=["The validated answer."],
                refused=False,
                model="fake",
                prompt_version="test",
                latency_ms=1,
                input_tokens=1,
                output_tokens=1,
                estimated_cost=0.0,
            ),
            retrieved_chunks=[],
            retrieval_latency_ms=0,
            model_latency_ms=0,
            source_citations=[self.source],
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
    safe_error = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == session.id,
            OutboxEvent.event_type == "chat.answer.safe_error",
        )
    )
    assert isinstance(safe_error, OutboxEvent)
    assert safe_error.payload["safe_body"] == messages[-1].body


@pytest.mark.asyncio
async def test_refusal_outbox_persists_exact_safe_body_and_customer_citation_projection(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    service = ChatAnswerService(db_session, RefusalWithStaffCitationService())
    _, job = await service.submit_customer_message(session.id, "Can you answer this?")
    await db_session.commit()

    await service.process(job.id)

    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == session.id,
            OutboxEvent.event_type == "chat.answer.refused",
        )
    )
    assert isinstance(event, OutboxEvent)
    assert event.payload["safe_body"] == "I don't know based on the available information."
    assert event.payload["citations"] == [
        {"title": "Customer-safe title", "section": "Overview", "page_number": 4}
    ]


@pytest.mark.asyncio
async def test_chat_outbox_persists_staff_sources_separately_from_customer_citations(
    db_session: AsyncSession,
) -> None:
    session = await _session(db_session)
    answer_service = ValidatedEvidenceAnswerService()
    service = ChatAnswerService(db_session, answer_service)
    _, job = await service.submit_customer_message(session.id, "Can you answer this?")
    await db_session.commit()

    await service.process(job.id)

    event = await db_session.scalar(
        select(OutboxEvent).where(
            OutboxEvent.aggregate_id == session.id,
            OutboxEvent.event_type == "chat.answer.validated",
        )
    )
    assert isinstance(event, OutboxEvent)
    assert event.payload["citations"] == [
        {"title": "Customer-safe title", "section": "Overview", "page_number": 4}
    ]
    assert event.payload["staff_citations"] == [answer_service.source.model_dump(mode="json")]


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
