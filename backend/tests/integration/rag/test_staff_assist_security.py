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
from app.modules.audit.models import AuditEvent
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.jobs.models import JobIntent
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
from app.modules.rag.evaluation_models import RAGEvaluationCase, RAGEvaluationRun
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import (
    GeneratedAnswer,
    InMemoryRedisCircuitStore,
    ProviderCircuitBreaker,
    ProviderTransientError,
)
from app.modules.rag.retriever import HybridRetriever
from app.modules.rag.types import ClaimSupport, RetrievedChunk


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


class TransientFailureProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, prompt: object) -> GeneratedAnswer:
        self.calls += 1
        raise ProviderTransientError("private upstream request id secret-provider-123")


class CountingHybridRetriever(HybridRetriever):
    calls = 0

    async def retrieve(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]:
        self.calls += 1
        return await super().retrieve(principal, knowledge_base_id, query, limit)


@dataclass(frozen=True)
class StaffAssistFixture:
    organization_id: UUID
    user_id: UUID
    staff_session_id: UUID
    cookie: str
    knowledge_base_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
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
        user_id=user.id,
        staff_session_id=staff_session.id,
        cookie=str(staff_session.id),
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        document_id=document.id,
        version_id=version.id,
        chunk_id=chunk_id,
    )


def _staff_app(
    provider: ContextClaimProvider | TransientFailureProvider,
    *,
    failure_threshold: int = 5,
) -> tuple[FastAPI, CountingHybridRetriever]:
    app = create_app()
    retriever = CountingHybridRetriever.from_session_factory(
        async_sessionmaker, StaticEmbeddingProvider()
    )
    app.state.grounded_answer_service = GroundedAnswerService(
        retriever,
        provider,
        CitationValidator(),
        ProviderCircuitBreaker(
            InMemoryRedisCircuitStore(), failure_threshold=failure_threshold
        ),
    )
    return app, retriever


async def _outbox_count() -> int:
    async with async_sessionmaker() as db_session:
        return int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)


async def _delete_organization(organization_id: UUID) -> None:
    async with async_sessionmaker() as db_session:
        await db_session.execute(delete(Organization).where(Organization.id == organization_id))
        await db_session.commit()


async def _mutable_domain_snapshot(fixture: StaffAssistFixture) -> dict[str, object]:
    async with async_sessionmaker() as db_session:
        organization = await db_session.get(Organization, fixture.organization_id)
        user = await db_session.get(StaffUser, fixture.user_id)
        staff_session = await db_session.get(StaffSession, fixture.staff_session_id)
        knowledge_base = await db_session.get(KnowledgeBase, fixture.knowledge_base_id)
        source = await db_session.get(DriveSource, fixture.source_id)
        document = await db_session.get(Document, fixture.document_id)
        version = await db_session.get(DocumentVersion, fixture.version_id)
        chunk = await db_session.get(DocumentChunk, fixture.chunk_id)
        grant = await db_session.scalar(
            select(ResourceGrant).where(
                ResourceGrant.organization_id == fixture.organization_id,
                ResourceGrant.subject_id == fixture.user_id,
                ResourceGrant.resource_id == fixture.knowledge_base_id,
            )
        )
        assert all(
            item is not None
            for item in (
                organization,
                user,
                staff_session,
                knowledge_base,
                source,
                document,
                version,
                chunk,
                grant,
            )
        )
        return {
            "organization": (organization.name,),  # type: ignore[union-attr]
            "user": (user.role, user.status, user.version),  # type: ignore[union-attr]
            "session": (staff_session.revoked_at, staff_session.expires_at),  # type: ignore[union-attr]
            "knowledge_base": (knowledge_base.default_language,),  # type: ignore[union-attr]
            "source": (
                source.root_folder_id,  # type: ignore[union-attr]
                source.status,  # type: ignore[union-attr]
                source.sync_cursor,  # type: ignore[union-attr]
            ),
            "document": (document.current_version_id, document.title),  # type: ignore[union-attr]
            "version": (version.state, version.error_code),  # type: ignore[union-attr]
            "chunk": (chunk.text, chunk.metadata_),  # type: ignore[union-attr]
            "grant": tuple(grant.actions),  # type: ignore[union-attr]
            "audit_count": int(
                await db_session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.organization_id == fixture.organization_id)
                )
                or 0
            ),
            "outbox_count": int(
                await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0
            ),
            "job_count": int(
                await db_session.scalar(select(func.count()).select_from(JobIntent)) or 0
            ),
            "evaluation_run_count": int(
                await db_session.scalar(select(func.count()).select_from(RAGEvaluationRun)) or 0
            ),
            "evaluation_case_count": int(
                await db_session.scalar(select(func.count()).select_from(RAGEvaluationCase)) or 0
            ),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(UserRole))
async def test_staff_assist_uses_real_hybrid_retrieval_for_every_spec_allowed_role(
    role: UserRole,
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=True, role=role)
        await db_session.commit()

    provider = ContextClaimProvider()
    before = await _outbox_count()
    app, retriever = _staff_app(provider)
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
        assert payload["citations"][0]["document_version_id"] == str(fixture.version_id)
        assert payload["citations"][0]["internal_drive_link"].startswith("https://drive.google.com/")
        assert "private upstream" not in response.text
        assert provider.calls == 1
        assert retriever.calls == 1
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_state", "unsupported"),
    [
        (DocumentVersionState.REVOKED, False),
        (DocumentVersionState.RETRIEVABLE, True),
    ],
)
async def test_staff_assist_grounded_and_authorization_failures_refuse_without_side_effects(
    version_state: DocumentVersionState,
    unsupported: bool,
) -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(
            db_session,
            grant_read=True,
            version_state=version_state,
        )
        await db_session.commit()

    provider = ContextClaimProvider(unsupported=unsupported)
    before = await _outbox_count()
    app, retriever = _staff_app(provider)
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
        assert "Refunds take one hour." not in response.text
        assert await _outbox_count() == before
        assert provider.calls == int(version_state is DocumentVersionState.RETRIEVABLE)
        assert retriever.calls == 1
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_staff_assist_denies_missing_application_authorization_before_retrieval() -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=False)
        await db_session.commit()

    provider = ContextClaimProvider()
    before = await _outbox_count()
    app, retriever = _staff_app(provider)
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

        assert response.status_code == 403
        assert provider.calls == 0
        assert retriever.calls == 0
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_staff_assist_provider_failure_is_redacted_and_opens_bounded_circuit() -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=True)
        await db_session.commit()

    provider = TransientFailureProvider()
    before = await _outbox_count()
    app, retriever = _staff_app(provider, failure_threshold=1)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"staff_session": fixture.cookie},
        ) as client:
            first = await client.post(
                "/api/v1/staff/knowledge/search", json={"question": "refund"}
            )
            retry = await client.post(
                "/api/v1/staff/knowledge/search", json={"question": "refund"}
            )

        assert first.status_code == retry.status_code == 200
        assert first.json()["refused"] is retry.json()["refused"] is True
        assert first.json()["citations"] == retry.json()["citations"] == []
        assert "secret-provider-123" not in first.text
        assert "secret-provider-123" not in retry.text
        assert provider.calls == 1
        assert retriever.calls == 2
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()


@pytest.mark.asyncio
async def test_staff_assist_success_is_read_only_across_mutable_platform_domains() -> None:
    async with async_sessionmaker() as db_session:
        fixture = await _seed_staff_assist(db_session, grant_read=True)
        await db_session.commit()

    provider = ContextClaimProvider()
    app, _ = _staff_app(provider)
    before = await _mutable_domain_snapshot(fixture)
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
        assert response.json()["refused"] is False
        assert await _mutable_domain_snapshot(fixture) == before
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
    app, retriever = _staff_app(provider)
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
        assert retriever.calls == 0
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
    app, retriever = _staff_app(provider)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
            cookies={"staff_session": fixture.cookie},
        ) as client:
            response = await client.post("/api/v1/staff/knowledge/search", json=payload)

        assert response.status_code == 422
        assert provider.calls == 0
        assert retriever.calls == 0
        assert await _outbox_count() == before
    finally:
        await _delete_organization(fixture.organization_id)
        await engine.dispose()
