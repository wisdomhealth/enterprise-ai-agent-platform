from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.chat.models import ChatActor, ChatMessage, ChatSession
from app.modules.identity.dependencies import Principal, get_db_session, require_staff_session
from app.modules.identity.models import Organization, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import KnowledgeBase
from app.modules.support.models import HandoffTrigger
from app.modules.support.service import SupportService


@pytest.mark.asyncio
async def test_staff_can_read_authorized_handoff_transcript_and_context(
    db_session: AsyncSession,
) -> None:
    organization = Organization(name=f"Support detail {uuid4()}")
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
    db_session.add_all(
        [
            ChatMessage(
                session_id=session.id, sequence=1, actor=ChatActor.CUSTOMER, body="Need help"
            ),
            ChatMessage(
                session_id=session.id, sequence=2, actor=ChatActor.AI, body="How can I help?"
            ),
        ]
    )
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
    handoff = await SupportService(db_session).request_handoff(
        session.id, trigger=HandoffTrigger.CUSTOMER_REQUEST
    )
    await db_session.commit()
    principal = Principal(
        reviewer.id, organization.id, reviewer.email, UserRole.REVIEWER, uuid4(), ""
    )
    app: FastAPI = create_app(Settings(SESSION_SECRET="staff-detail-secret"))

    async def override_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def override_staff() -> Principal:
        return principal

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[require_staff_session] = override_staff
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.get(f"/api/v1/staff/support/{handoff.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["trigger"] == "CUSTOMER_REQUEST"
    assert payload["customer"] == {"name": "Ada", "email": "ada@example.test"}
    assert [(message["sequence"], message["body"]) for message in payload["messages"]] == [
        (1, "Need help"),
        (2, "How can I help?"),
    ]
