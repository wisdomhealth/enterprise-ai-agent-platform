from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker
from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ConversationState
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase
from app.modules.outbox.models import OutboxEvent
from app.modules.outbox.service import OutboxService
from app.modules.rag.types import ClaimSupport, ValidatedAnswer
from app.modules.support.models import Handoff, HandoffTrigger, SensitiveTopic


class RefusalAnswer:
    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        return ValidatedAnswer(
            text="I don't know.",
            claims=[],
            citations=[],
            segments=["I don't know."],
            refused=True,
            model="test",
            prompt_version="test",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.0,
        )


class ValidAnswer:
    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        return ValidatedAnswer(
            text="A supported answer.",
            claims=[ClaimSupport(text="A supported answer.", citation_ids=[], material=True)],
            citations=[],
            segments=["A supported answer."],
            refused=False,
            model="test",
            prompt_version="test",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0.0,
        )


class SafetyTopicClassifier:
    async def classify(self, _text: str) -> SensitiveTopic | None:
        return SensitiveTopic.ACCOUNT_SECURITY


class NoTopicClassifier:
    async def classify(self, _text: str) -> SensitiveTopic | None:
        return None


class HumanTakeoverThenFailure:
    def __init__(self, session_id) -> None:  # type: ignore[no-untyped-def]
        self._session_id = session_id

    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        async with async_sessionmaker() as takeover:
            await takeover.execute(
                update(ChatSession)
                .where(ChatSession.id == self._session_id)
                .values(state=ConversationState.HUMAN_ACTIVE)
            )
            await takeover.commit()
        raise RuntimeError("provider failed after human takeover")


@pytest.mark.asyncio
async def test_refusal_persists_then_queues_low_confidence_handoff(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Answer trigger {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(organization_id=organization.id, knowledge_base_id=knowledge_base.id)
    db_session.add(session)
    await db_session.flush()
    service = ChatAnswerService(db_session, RefusalAnswer())
    _, job = await service.submit_customer_message(session.id, "Can you answer?")
    await db_session.commit()

    await service.process(job.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.session_id == session.id))
    assert isinstance(handoff, Handoff)
    assert handoff.trigger is HandoffTrigger.LOW_CONFIDENCE
    assert handoff.state is ConversationState.QUEUED


@pytest.mark.asyncio
async def test_two_persisted_refused_ai_answers_trigger_repeated_failure(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Repeated failure {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(organization_id=organization.id, knowledge_base_id=knowledge_base.id)
    db_session.add(session)
    await db_session.flush()
    old_refusal = ChatMessage(
        session_id=session.id, sequence=1, actor=ChatActor.AI, body="Old refusal."
    )
    db_session.add(old_refusal)
    await db_session.flush()
    await OutboxService().add(
        db_session,
        "chat.answer.refused",
        "chat_session",
        session.id,
        {
            "message_id": str(old_refusal.id),
            "sequence": old_refusal.sequence,
            "refused": True,
        },
    )
    service = ChatAnswerService(db_session, RefusalAnswer())
    _, job = await service.submit_customer_message(session.id, "Try again")
    await db_session.commit()

    await service.process(job.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.session_id == session.id))
    assert isinstance(handoff, Handoff)
    assert handoff.trigger is HandoffTrigger.REPEATED_FAILURE


@pytest.mark.asyncio
async def test_structured_classifier_controls_sensitive_topic_handoff(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Structured safety {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(organization_id=organization.id, knowledge_base_id=knowledge_base.id)
    db_session.add(session)
    await db_session.flush()
    service = ChatAnswerService(
        db_session, ValidAnswer(), safety_classifier=SafetyTopicClassifier()
    )
    _, job = await service.submit_customer_message(session.id, "ordinary customer text")
    await db_session.commit()

    await service.process(job.id)

    handoff = await db_session.scalar(select(Handoff).where(Handoff.session_id == session.id))
    assert isinstance(handoff, Handoff)
    assert handoff.trigger is HandoffTrigger.SENSITIVE_TOPIC
    assert handoff.sensitive_topic is SensitiveTopic.ACCOUNT_SECURITY


@pytest.mark.asyncio
async def test_keywords_alone_do_not_replace_structured_safety_classification(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"No keyword safety {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(organization_id=organization.id, knowledge_base_id=knowledge_base.id)
    db_session.add(session)
    await db_session.flush()
    service = ChatAnswerService(db_session, ValidAnswer(), safety_classifier=NoTopicClassifier())
    _, job = await service.submit_customer_message(session.id, "My password is forgotten")
    await db_session.commit()

    await service.process(job.id)

    assert await db_session.scalar(select(Handoff).where(Handoff.session_id == session.id)) is None


@pytest.mark.asyncio
async def test_human_takeover_blocks_stale_system_safe_error_publication() -> None:
    # This scenario must use independently committed sessions: the ordinary
    # db_session fixture owns an outer rollback transaction, so a second
    # connection cannot observe its setup rows and would not perform a real
    # takeover.
    async with async_sessionmaker() as setup_session:
        organization = Organization(name=f"Failure takeover {uuid4()}")
        setup_session.add(organization)
        await setup_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id, public_key=f"public-{uuid4().hex}"
        )
        setup_session.add(knowledge_base)
        await setup_session.flush()
        session = ChatSession(
            organization_id=organization.id, knowledge_base_id=knowledge_base.id
        )
        setup_session.add(session)
        await setup_session.flush()
        service = ChatAnswerService(
            setup_session, HumanTakeoverThenFailure(session.id)
        )
        _, job = await service.submit_customer_message(session.id, "Please help")
        session_id = session.id
        job_id = job.id
        await setup_session.commit()

    async with async_sessionmaker() as worker_session:
        result = await ChatAnswerService(
            worker_session, HumanTakeoverThenFailure(session_id)
        ).process(job_id)

    async with async_sessionmaker() as verification_session:
        persisted_session = await verification_session.get(ChatSession, session_id)
        system_messages = list(
            (
                await verification_session.scalars(
                select(ChatMessage).where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.actor == ChatActor.SYSTEM,
                )
            )
            ).all()
        )
        safe_error_events = list(
            (
                await verification_session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == session_id,
                        OutboxEvent.event_type == "chat.answer.safe_error",
                    )
                )
            ).all()
        )
    assert isinstance(persisted_session, ChatSession)
    assert persisted_session.state is ConversationState.HUMAN_ACTIVE
    assert result is None
    assert system_messages == []
    assert safe_error_events == []
