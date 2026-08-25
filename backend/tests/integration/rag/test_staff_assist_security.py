from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_sessionmaker, engine
from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.outbox.models import OutboxEvent
from app.modules.rag.answer_service import GroundedAnswerService
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import GeneratedAnswer, InMemoryRedisCircuitStore, ProviderCircuitBreaker
from app.modules.rag.retriever import HybridRetriever
from app.modules.rag.types import ClaimSupport


class StaticEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1536 for _ in texts]


class ContextClaimProvider:
    """Deterministic stand-in for the external answer provider only."""

    def __init__(self, *, unsupported: bool = False) -> None:
        self.unsupported = unsupported
        self.calls = 0

    async def generate(self, prompt: object) -> GeneratedAnswer:
        self.calls += 1
        user_message = getattr(prompt, "user_message")
        chunk_id = UUID(user_message.split('"chunk_id":"', 1)[1].split('"', 1)[0])
        text = "Refunds take one hour." if self.unsupported else "Refunds take five business days."
        return GeneratedAnswer(
            text=text,
            claims=[ClaimSupport(text=text, citation_ids=[chunk_id])],
            model="test-provider",
            input_tokens=1,
            output_tokens=1,
        )


@dataclass(frozen=True)
class StaffAssistFixture:
    organization_id: UUID
    cookie: str
    knowledge_base_id: UUID
    chunk_id: UUID


async def _seed_staff_assist(
    db_session: AsyncSession,
    *,
    grant_read: bool,
    role: UserRole = UserRole.REVIEWER,
    version_state: DocumentVersionState = DocumentVersionState.RETRIEVABLE,
    user_status: UserStatus = UserStatus.ACTIVE,
    session_expired: bool = False,
    session_revoked: bool = False,
) -> StaffAssistFixture:
    organization = Organization(name=f"staff-assist {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"staff-{uuid4()}",
        email="staff@example.test",
        role=role,
        status=user_status,
    )
    db_session.add(user)
    await db_session.flush()
    csrf_token = "staff-assist-csrf"
    staff_session = StaffSession(
        user_id=user.id,
        csrf_hash=sha256(csrf_token.encode()).hexdigest(),
        expires_at=(
            datetime.now(UTC) - timedelta(minutes=1)
            if session_expired
            else datetime.now(UTC) + timedelta(hours=1)
        ),
        revoked_at=datetime.now(UTC) if session_revoked else None,
    )
    db_session.add(staff_session)
    knowledge_base = KnowledgeBase(organization_id=organization.id)
    db_session.add(knowledge_base)
    await db_session.flush()
    source = DriveSource(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        root_folder_id="root",
        connection_identity="reader@example.test",
    )
    db_session.add(source)
    await db_session.flush()
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id=str(uuid4()),
        title="Refund policy",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=version_state,
        content_sha256=uuid4().hex + uuid4().hex,
    )
    db_session.add(version)
    await db_session.flush()
    document.current_version_id = version.id
    chunk_id = uuid4()
    db_session.add(
        DocumentChunk(
            id=chunk_id,
            document_version_id=version.id,
            ordinal=0,
            text="Refunds take five business days.",
            page_number=2,
            section="Eligibility",
            token_count=5,
            metadata_={},
            embedding=[1.0] * 1536,
        )
    )
    if grant_read:
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.read"],
            )
        )
    await db_session.flush()
    return StaffAssistFixture(
        organization_id=organization.id,
        cookie=str(staff_session.id),
        knowledge_base_id=knowledge_base.id,
        chunk_id=chunk_id,
    )


def _staff_app(provider: ContextClaimProvider) -> FastAPI:
    app = create_app()
    app.state.grounded_answer_service = GroundedAnswerService(
        HybridRetriever.from_session_factory(async_sessionmaker, StaticEmbeddingProvider()),
        provider,
        CitationValidator(),
        ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
    )
    return app


async def _outbox_count() -> int:
    async with async_sessionmaker() as db_session:
        return int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)


async def _delete_organization(organization_id: UUID) -> None:
    async with async_sessionmaker() as db_session:
        await db_session.execute(delete(Organization).where(Organization.id == organization_id))
        await db_session.commit()


@pytest.mark.asyncio
async def test_staff_assist_uses_real_hybrid_retrieval_for_authenticated_reviewer_read_only(
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=True)
        await db_session.commit()

    provider = ContextClaimProvider()
    before = await _outbox_count()
    app = _staff_app(provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"staff_session": fixture.cookie},
        ) as client:
            response = await client.post(
                "/api/v1/staff/knowledge/search",
                json={"question": "What is the refund policy?"},
            )

        assert response.status_code == 200
        payload = response.json()
        assert payload["refused"] is False
        assert payload["citations"][0]["chunk_id"] == str(fixture.chunk_id)
        assert payload["citations"][0]["internal_drive_link"].startswith("https://drive.google.com/")
        assert provider.calls == 1
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grant_read", "version_state", "unsupported"),
    [
        (False, DocumentVersionState.RETRIEVABLE, False),
        (True, DocumentVersionState.REVOKED, False),
        (True, DocumentVersionState.RETRIEVABLE, True),
    ],
)
async def test_staff_assist_grounded_and_authorization_failures_refuse_without_side_effects(
    grant_read: bool,
    version_state: DocumentVersionState,
    unsupported: bool,
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(
            db_session,
            grant_read=grant_read,
            version_state=version_state,
        )
        await db_session.commit()

    provider = ContextClaimProvider(unsupported=unsupported)
    before = await _outbox_count()
    app = _staff_app(provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"staff_session": fixture.cookie},
        ) as client:
            response = await client.post(
                "/api/v1/staff/knowledge/search",
                json={"question": "refund"},
            )

        assert response.status_code == 200
        assert response.json()["refused"] is True
        assert response.json()["citations"] == []
        assert await _outbox_count() == before
        assert provider.calls == int(
            grant_read and version_state is DocumentVersionState.RETRIEVABLE
        )
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_expired", "session_revoked", "user_status", "cookie"),
    [
        (False, False, UserStatus.ACTIVE, None),
        (False, False, UserStatus.ACTIVE, "not-a-uuid"),
        (True, False, UserStatus.ACTIVE, "seeded"),
        (False, True, UserStatus.ACTIVE, "seeded"),
        (False, False, UserStatus.DISABLED, "seeded"),
    ],
)
async def test_staff_assist_rejects_invalid_or_inactive_staff_sessions_without_provider_calls(
    session_expired: bool,
    session_revoked: bool,
    user_status: UserStatus,
    cookie: str | None,
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(
            db_session,
            grant_read=True,
            session_expired=session_expired,
            session_revoked=session_revoked,
            user_status=user_status,
        )
        await db_session.commit()

    provider = ContextClaimProvider()
    before = await _outbox_count()
    app = _staff_app(provider)
    resolved_cookie = fixture.cookie if cookie == "seeded" else cookie
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={} if resolved_cookie is None else {"staff_session": resolved_cookie},
        ) as client:
            response = await client.post(
                "/api/v1/staff/knowledge/search",
                json={"question": "refund"},
            )

        assert response.status_code == 401
        assert provider.calls == 0
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"question": ""},
        {"question": "refund", "unexpected": True},
    ],
)
async def test_staff_assist_rejects_invalid_request_bodies_without_provider_calls(
    payload: dict[str, object],
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=True)
        await db_session.commit()

    provider = ContextClaimProvider()
    before = await _outbox_count()
    app = _staff_app(provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"staff_session": fixture.cookie},
        ) as client:
            response = await client.post("/api/v1/staff/knowledge/search", json=payload)

        assert response.status_code == 422
        assert provider.calls == 0
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()
