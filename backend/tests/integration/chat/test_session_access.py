import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.database import async_sessionmaker, engine
from app.main import create_app
from app.modules.chat.models import ChatSession, ChatSessionCredential
from app.modules.chat.rate_limit import SlidingWindowRateLimiter
from app.modules.chat.service import ChatSessionService
from app.modules.chat.tokens import ChatTokenService
from app.modules.idempotency.models import IdempotencyRecord
from app.modules.identity.dependencies import get_db_session
from app.modules.identity.models import Organization
from app.modules.knowledge.models import KnowledgeBase


class AlwaysAdmitRedis:
    async def eval(self, _script: str, _numkeys: int, *_args: object) -> list[int]:
        return [1, 0]


@pytest.fixture
def app(db_session: AsyncSession) -> FastAPI:
    application = create_app(Settings(SESSION_SECRET="task-thirteen-test-session-secret"))

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


@pytest.mark.asyncio
async def test_public_writes_fail_closed_when_redis_is_not_configured(public_client, db_session):
    organization = Organization(name="Unavailable limiter")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{organization.id.hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()

    response = await public_client.post(
        "/api/v1/public/chat/sessions",
        json={"public_key": knowledge_base.public_key},
        headers={"Idempotency-Key": "no-redis"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"] == "Please wait a moment before trying again."


@pytest.mark.asyncio
async def test_public_writes_require_an_idempotency_key(
    app: FastAPI, public_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    organization = Organization(name="Required idempotency key")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{organization.id.hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()

    response = await public_client.post(
        "/api/v1/public/chat/sessions", json={"public_key": knowledge_base.public_key}
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_credential_rotation_requires_an_idempotency_key(
    app: FastAPI, public_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    session, token = await _session_with_token(db_session)

    response = await public_client.post(
        f"/api/v1/public/chat/sessions/{session.id}/credentials/rotate",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_public_writes_reject_when_session_secret_is_not_configured(
    app: FastAPI, public_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app.state.settings = Settings()
    app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    organization = Organization(name="Missing chat session secret")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{organization.id.hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()

    response = await public_client.post(
        "/api/v1/public/chat/sessions",
        json={"public_key": knowledge_base.public_key},
        headers={"Idempotency-Key": "missing-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Public chat is unavailable."


@pytest.mark.asyncio
async def test_session_creation_idempotency_replays_original_body_and_rejects_mismatch(
    app: FastAPI, public_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    organization = Organization(name="Creation idempotency")
    db_session.add(organization)
    await db_session.flush()
    knowledge_base = KnowledgeBase(
        organization_id=organization.id,
        public_key=f"public-{organization.id.hex}",
    )
    db_session.add(knowledge_base)
    await db_session.flush()
    request = {"public_key": knowledge_base.public_key, "customer_name": "Ada"}

    first = await public_client.post(
        "/api/v1/public/chat/sessions", json=request, headers={"Idempotency-Key": "create-1"}
    )
    replay = await public_client.post(
        "/api/v1/public/chat/sessions", json=request, headers={"Idempotency-Key": "create-1"}
    )
    mismatch = await public_client.post(
        "/api/v1/public/chat/sessions",
        json={**request, "customer_name": "Grace"},
        headers={"Idempotency-Key": "create-1"},
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert mismatch.status_code == 409
    record = await db_session.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.key == "create-1")
    )
    assert record is not None
    assert first.json()["credential"]["token"] not in str(record.response_body)
    assert "task-thirteen-test-session-secret" not in str(record.response_body)
    assert "token" not in (record.response_body or {}).get("credential", {})


@pytest.mark.asyncio
async def test_rotation_replay_rejects_the_revoked_old_credential(
    app: FastAPI, public_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    session, old_token = await _session_with_token(db_session)
    headers = {"Authorization": f"Bearer {old_token}", "Idempotency-Key": "rotate-1"}

    first = await public_client.post(
        f"/api/v1/public/chat/sessions/{session.id}/credentials/rotate", headers=headers
    )
    replay = await public_client.post(
        f"/api/v1/public/chat/sessions/{session.id}/credentials/rotate", headers=headers
    )

    assert first.status_code == 200
    assert replay.status_code == 404
    assert "token" not in replay.text
    assert (await public_client.get(
        f"/api/v1/public/chat/sessions/{session.id}",
        headers={"Authorization": f"Bearer {old_token}"},
    )).status_code == 404
    current = await public_client.post(
        f"/api/v1/public/chat/sessions/{session.id}/credentials/rotate",
        headers={
            "Authorization": f"Bearer {first.json()['token']}",
            "Idempotency-Key": "rotate-current-1",
        },
    )
    assert current.status_code == 200
    record = await db_session.scalar(
        select(IdempotencyRecord).where(IdempotencyRecord.key == "rotate-1")
    )
    assert record is not None
    assert first.json()["token"] not in str(record.response_body)
    assert "task-thirteen-test-session-secret" not in str(record.response_body)
    assert "token" not in (record.response_body or {}).get("credential", {})


@pytest.mark.asyncio
async def test_idempotency_replay_is_durable_across_fresh_app_instances() -> None:
    session_secret = "durable-public-chat-session-secret"
    async with async_sessionmaker() as setup_session:
        organization = Organization(name="Durable idempotency")
        setup_session.add(organization)
        await setup_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id,
            public_key=f"public-{organization.id.hex}",
        )
        setup_session.add(knowledge_base)
        await setup_session.commit()

    first_app = create_app(Settings(SESSION_SECRET=session_secret))
    first_app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    request = {"public_key": knowledge_base.public_key, "customer_name": "Ada"}
    headers = {"Idempotency-Key": "durable-create"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app), base_url="https://testserver"
    ) as first_client:
        first = await first_client.post(
            "/api/v1/public/chat/sessions", json=request, headers=headers
        )

    fresh_app = create_app(Settings(SESSION_SECRET=session_secret))
    fresh_app.state.chat_rate_limiter = SlidingWindowRateLimiter(AlwaysAdmitRedis())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fresh_app), base_url="https://testserver"
    ) as fresh_client:
        replay = await fresh_client.post(
            "/api/v1/public/chat/sessions", json=request, headers=headers
        )

    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    async with async_sessionmaker() as verification_session:
        record = await verification_session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key == "durable-create")
        )
    assert record is not None
    assert first.json()["credential"]["token"] not in str(record.response_body)
    assert session_secret not in str(record.response_body)
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_old_token_rotations_leave_exactly_one_active_replacement() -> None:
    async with async_sessionmaker() as setup_session:
        organization = Organization(name="Concurrent rotation")
        setup_session.add(organization)
        await setup_session.flush()
        knowledge_base = KnowledgeBase(
            organization_id=organization.id,
            public_key=f"public-{organization.id.hex}",
        )
        setup_session.add(knowledge_base)
        await setup_session.flush()
        session = ChatSession(
            organization_id=organization.id,
            knowledge_base_id=knowledge_base.id,
        )
        setup_session.add(session)
        await setup_session.flush()
        issued = ChatTokenService().issue(session_id=session.id, lifetime_seconds=3_600)
        setup_session.add(
            ChatSessionCredential(
                session_id=session.id,
                token_hash=issued.token_hash,
                expires_at=issued.expires_at,
            )
        )
        await setup_session.commit()

    async def rotate_once() -> tuple[str, datetime] | None:
        async with async_sessionmaker() as rotation_session:
            result = await ChatSessionService(rotation_session).rotate_credential(
                session_id=session.id,
                credential_value=issued.value,
                replacement_credential_value=token_urlsafe(32),
            )
            await rotation_session.commit()
            return result

    first, second = await asyncio.gather(rotate_once(), rotate_once())
    assert sum(result is not None for result in (first, second)) == 1

    async with async_sessionmaker() as verification_session:
        credentials = list(
            (
                await verification_session.scalars(
                    select(ChatSessionCredential).where(
                        ChatSessionCredential.session_id == session.id
                    )
                )
            ).all()
        )
    assert len(credentials) == 2
    assert sum(credential.revoked_at is None for credential in credentials) == 1


def test_task_thirteen_does_not_publish_the_task_fourteen_message_route() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/api/v1/public/chat/sessions/{session_id}/messages" not in paths
