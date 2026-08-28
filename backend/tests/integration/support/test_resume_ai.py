from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession, ConversationState
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent, JobState
from app.modules.knowledge.models import KnowledgeBase
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService


@pytest.mark.asyncio
async def test_resume_ai_clears_pending_old_answer_and_waits_for_later_customer_message(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Support resume {uuid4()}")
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
    customer = ChatMessage(session_id=session.id, sequence=1, actor=ChatActor.CUSTOMER, body="help")
    db_session.add(customer)
    await db_session.flush()
    job = JobIntent(
        kind="chat.answer",
        idempotency_key=f"support-{uuid4()}",
        payload={"session_id": str(session.id), "message_id": str(customer.id)},
        state=JobState.PENDING,
    )
    db_session.add(job)
    await db_session.flush()
    service = SupportService(db_session)
    handoff = await service.request_handoff(session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST)
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"support-{uuid4()}",
        email=f"reviewer-{uuid4()}@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(reviewer)
    await db_session.flush()
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=reviewer.id,
            resource_type="knowledge",
            resource_id=knowledge_base.id,
            actions=["knowledge.review"],
        )
    )
    await db_session.flush()
    principal = Principal(
        reviewer.id, organization.id, reviewer.email, UserRole.REVIEWER, uuid4(), ""
    )
    claimed = await service.claim(handoff.id, principal, handoff.version)
    resumed = await service.resume_ai(handoff.id, principal, claimed.version)
    assert resumed.state is ConversationState.AI_ACTIVE
    assert (await db_session.get(JobIntent, job.id)).state is JobState.FAILED  # type: ignore[union-attr]
    assert (
        list(
            (
                await db_session.scalars(
                    select(ChatMessage).where(
                        ChatMessage.session_id == session.id, ChatMessage.actor == ChatActor.AI
                    )
                )
            ).all()
        )
        == []
    )


@pytest.mark.asyncio
async def test_customer_can_create_fresh_handoff_after_explicit_resume(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Support rehandoff {uuid4()}")
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
    reviewer = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"support-{uuid4()}",
        email=f"reviewer-{uuid4()}@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(reviewer)
    await db_session.flush()
    db_session.add(
        ResourceGrant(
            organization_id=organization.id,
            subject_id=reviewer.id,
            resource_type="knowledge",
            resource_id=knowledge_base.id,
            actions=["knowledge.review"],
        )
    )
    await db_session.flush()
    principal = Principal(
        reviewer.id, organization.id, reviewer.email, UserRole.REVIEWER, uuid4(), ""
    )
    service = SupportService(db_session)
    first = await service.request_handoff(session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST)
    claimed = await service.claim(first.id, principal, first.version)
    await service.resume_ai(first.id, principal, claimed.version)
    db_session.add(
        ChatMessage(
            session_id=session.id,
            sequence=1,
            actor=ChatActor.CUSTOMER,
            body="Need help again",
        )
    )
    await db_session.flush()

    second = await service.request_handoff(session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST)

    assert second.id != first.id
    assert second.state is ConversationState.QUEUED
    assert second.snapshot["last_customer_sequence"] == 1
