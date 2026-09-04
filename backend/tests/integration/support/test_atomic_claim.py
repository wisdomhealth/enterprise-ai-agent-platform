import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker, engine
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import ChatSession
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import ClaimedHandoff, SupportService, VersionConflict


async def _session(db_session: AsyncSession) -> ChatSession:
    organization = Organization(name=f"Support claim {uuid4()}")
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
    return session


async def _reviewer(db_session: AsyncSession, session: ChatSession) -> Principal:
    user = StaffUser(
        organization_id=session.organization_id,
        oidc_subject=f"support-{uuid4()}",
        email=f"reviewer-{uuid4()}@example.test",
        role=UserRole.REVIEWER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        ResourceGrant(
            organization_id=session.organization_id,
            subject_id=user.id,
            resource_type="knowledge",
            resource_id=session.knowledge_base_id,
            actions=["knowledge.review"],
        )
    )
    await db_session.flush()
    return Principal(user.id, session.organization_id, user.email, UserRole.REVIEWER, uuid4(), "")


@pytest.mark.asyncio
async def test_second_claim_with_stale_version_conflicts(db_session: AsyncSession) -> None:
    session = await _session(db_session)
    service = SupportService(db_session)
    handoff = await service.request_handoff(session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST)
    first = await service.claim(handoff.id, await _reviewer(db_session, session), handoff.version)
    assert isinstance(first, ClaimedHandoff)
    with pytest.raises(VersionConflict):
        await service.claim(handoff.id, await _reviewer(db_session, session), handoff.version)


@pytest.mark.asyncio
async def test_two_reviewers_cannot_claim_same_handoff_across_sessions() -> None:
    async with async_sessionmaker() as setup:
        session = await _session(setup)
        reviewer_a = await _reviewer(setup, session)
        reviewer_b = await _reviewer(setup, session)
        handoff = await SupportService(setup).request_handoff(
            session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
        )
        handoff_id, version = handoff.id, handoff.version
        await setup.commit()

    async def claim(principal: Principal) -> object:
        async with async_sessionmaker() as worker:
            try:
                result = await SupportService(worker).claim(handoff_id, principal, version)
                await worker.commit()
                return result
            except VersionConflict as error:
                await worker.rollback()
                return error

    try:
        results = await asyncio.gather(claim(reviewer_a), claim(reviewer_b))
    finally:
        await engine.dispose()
    assert sum(isinstance(item, ClaimedHandoff) for item in results) == 1
    assert sum(isinstance(item, VersionConflict) for item in results) == 1
