from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.main import create_app
from app.modules.chat.models import ChatSession, ChatSessionCredential, ConversationState
from app.modules.chat.tokens import ChatTokenService
from app.modules.identity.dependencies import get_db_session
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase


@pytest.mark.asyncio
async def test_public_handoff_is_bearer_bound_and_idempotently_queues_contact(
    db_session: AsyncSession,
) -> None:
    app: FastAPI = create_app(Settings(SESSION_SECRET="support-route-secret"))

    async def override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = override
    organization = Organization(name=f"Public handoff {uuid4()}")
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
    issued = ChatTokenService().issue(session_id=session.id)
    db_session.add(
        ChatSessionCredential(
            session_id=session.id, token_hash=issued.token_hash, expires_at=issued.expires_at
        )
    )
    await db_session.commit()

    headers = {"Authorization": f"Bearer {issued.value}", "Idempotency-Key": "handoff-1"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        first = await client.post(
            f"/api/v1/public/chat/sessions/{session.id}/handoff",
            headers=headers,
            json={"contact_name": "Ada", "contact_email": "ada@example.test"},
        )
        replay = await client.post(
            f"/api/v1/public/chat/sessions/{session.id}/handoff",
            headers=headers,
            json={"contact_name": "Ada", "contact_email": "ada@example.test"},
        )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["state"] == ConversationState.QUEUED
