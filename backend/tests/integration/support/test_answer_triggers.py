from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatSession, ConversationState
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase
from app.modules.rag.types import ValidatedAnswer
from app.modules.support.models import Handoff, HandoffTrigger


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
