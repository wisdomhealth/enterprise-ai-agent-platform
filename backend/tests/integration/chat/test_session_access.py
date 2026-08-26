from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.chat.models import ChatSession, ChatSessionCredential
from app.modules.chat.tokens import ChatTokenService
from app.modules.identity.dependencies import get_db_session
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    application = create_app()

    async def override_db_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = override_db_session
    return application


@pytest_asyncio.fixture
async def public_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        yield client


async def _session_with_token(
    db_session: AsyncSession, *, expires_at: datetime | None = None
) -> tuple[ChatSession, str]:
    organization = Organization(name="Public chat test")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{organization.id.hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    session = ChatSession(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
    )
    db_session.add(session)
    await db_session.flush()
    issued = ChatTokenService().issue(session_id=session.id, lifetime_seconds=3_600)
    db_session.add(
        ChatSessionCredential(
            session_id=session.id,
            token_hash=issued.token_hash,
            expires_at=expires_at or issued.expires_at,
        )
    )
    await db_session.flush()
    return session, issued.value


@pytest.mark.asyncio
async def test_anonymous_token_cannot_read_another_session(public_client, db_session):
    session_a, _ = await _session_with_token(db_session)
    _, token_b = await _session_with_token(db_session)

    response = await public_client.get(
        f"/api/v1/public/chat/sessions/{session_a.id}",
        headers={"Authorization": f"Bearer {token_b}"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_expired_token_is_rejected_and_public_read_hides_internal_fields(
    public_client, db_session
):
    session, expired_token = await _session_with_token(
        db_session, expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )

    expired = await public_client.get(
        f"/api/v1/public/chat/sessions/{session.id}",
        headers={"Authorization": f"Bearer {expired_token}"},
    )
    assert expired.status_code == 404

    _, valid_token = await _session_with_token(db_session)
    valid = await public_client.get(
        f"/api/v1/public/chat/sessions/{session.id}",
        headers={"Authorization": f"Bearer {valid_token}"},
    )
    assert valid.status_code == 404
    assert "knowledge_base_id" not in valid.text
    assert "organization_id" not in valid.text
