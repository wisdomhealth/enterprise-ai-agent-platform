from uuid import uuid4

import pytest
from sqlalchemy import select

from app.modules.chat.answering import ChatAnswerService
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ConversationState
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase
from app.modules.rag.types import ValidatedAnswer
from app.modules.support.models import Handoff, HandoffTrigger
from app.modules.support.service import SupportService


class LateAnswer:
    def __init__(self, db_session, session_id) -> None:  # type: ignore[no-untyped-def]
        self._db_session = db_session
        self._session_id = session_id

    async def answer(self, *_args: object, **_kwargs: object) -> ValidatedAnswer:
        await SupportService(self._db_session).request_handoff(
            self._session_id, trigger=HandoffTrigger.CUSTOMER_REQUEST
        )
        return ValidatedAnswer(
            text="stale AI answer",
            claims=[],
            citations=[],
            segments=["stale AI answer"],
            refused=False,
            model="fake",
            prompt_version="fake-v1",
            latency_ms=1,
            input_tokens=1,
            output_tokens=1,
            estimated_cost=0,
        )


@pytest.mark.asyncio
async def test_handoff_fences_late_ai_output_before_publication(db_session) -> None:  # type: ignore[no-untyped-def]
    organization = Organization(name=f"Task 26 handoff {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge)
    await db_session.flush()
    session = ChatSession(organization_id=organization.id, knowledge_base_id=knowledge.id)
    db_session.add(session)
    await db_session.flush()
    service = ChatAnswerService(db_session, LateAnswer(db_session, session.id))
    _, job = await service.submit_customer_message(session.id, "Please get a person")
    await db_session.commit()

    result = await service.process(job.id)

    assert result is None
    assert (await db_session.get(ChatSession, session.id)).state is ConversationState.QUEUED
    assert await db_session.scalar(select(Handoff).where(Handoff.session_id == session.id))
    answers = list(
        (
            await db_session.scalars(
                select(ChatMessage).where(
                    ChatMessage.session_id == session.id,
                    ChatMessage.actor.in_((ChatActor.AI, ChatActor.SYSTEM)),
                )
            )
        ).all()
    )
    assert answers == []
