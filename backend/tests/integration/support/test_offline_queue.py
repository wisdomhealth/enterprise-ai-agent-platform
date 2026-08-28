from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import ChatSession
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService


@pytest.mark.asyncio
async def test_handoff_snapshot_keeps_optional_offline_contact_details(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Offline queue {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id, public_key=f"public-{uuid4().hex}"
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        customer_name="Ada",
        customer_email="ada@example.test",
    )
    db_session.add(session)
    await db_session.flush()
    handoff = await SupportService(db_session).request_handoff(
        session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
    )
    assert handoff.snapshot["customer"] == {"name": "Ada", "email": "ada@example.test"}
