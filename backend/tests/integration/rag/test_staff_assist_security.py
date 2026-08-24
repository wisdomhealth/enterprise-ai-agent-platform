from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import create_app
from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import get_db_session
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
from app.modules.rag.text_search import TextCandidateSource
from app.modules.rag.types import ClaimSupport


class TextOnlyRetriever:
    def __init__(self, session: AsyncSession) -> None:
        self._source = TextCandidateSource(session)

    async def retrieve(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        return await self._source.search(*args, **kwargs)


class ContextClaimProvider:
    def __init__(self, *, unsupported: bool = False) -> None:
        self.unsupported = unsupported
        self.calls = 0

    async def generate(self, prompt):  # type: ignore[no-untyped-def]
        self.calls += 1
        chunk_id = UUID(prompt.user_message.split('"chunk_id":"', 1)[1].split('"', 1)[0])
        text = "Refunds take one hour." if self.unsupported else "Refunds take five business days."
        return GeneratedAnswer(
            text=text,
            claims=[ClaimSupport(text=text, citation_ids=[chunk_id])],
            model="test-provider",
            input_tokens=1,
            output_tokens=1,
        )


async def _seed_staff_assist(  # type: ignore[no-untyped-def]
    db_session,
    *,
    grant_read: bool,
    version_state: DocumentVersionState = DocumentVersionState.RETRIEVABLE,
):
    organization = Organization(name=f"staff-assist {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"staff-{uuid4()}",
        email="staff@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    csrf_token = "staff-assist-csrf"
    staff_session = StaffSession(
        user_id=user.id,
        csrf_hash=sha256(csrf_token.encode()).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
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
    document = Document(
        organization_id=organization.id,
        knowledge_base_id=knowledge_base.id,
        source_id=source.id,
        external_id=str(uuid4()),
        title="Refund policy",
        mime_type="application/pdf",
    )
    db_session.add(source)
    await db_session.flush()
    document.source_id = source.id
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
    db_session.add(
        DocumentChunk(
            id=uuid4(),
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
    return str(staff_session.id)


async def _staff_client(  # type: ignore[no-untyped-def]
    db_session, provider: ContextClaimProvider
) -> httpx.AsyncClient:
    app: FastAPI = create_app()

    async def override_db_session():  # type: ignore[no-untyped-def]
        yield db_session

    app.dependency_overrides[get_db_session] = override_db_session
    app.state.grounded_answer_service = GroundedAnswerService(
        TextOnlyRetriever(db_session),
        provider,
        CitationValidator(),
        ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver")


async def _outbox_count(db_session: AsyncSession) -> int:
    return int(await db_session.scalar(select(func.count()).select_from(OutboxEvent)) or 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("grant_read", "version_state", "unsupported"),
    [
        (False, DocumentVersionState.RETRIEVABLE, False),
        (True, DocumentVersionState.REVOKED, False),
        (True, DocumentVersionState.RETRIEVABLE, True),
    ],
)
async def test_staff_assist_production_auth_and_grounded_failures_are_read_only(
    db_session,
    grant_read: bool,
    version_state: DocumentVersionState,
    unsupported: bool,
) -> None:
    cookie = await _seed_staff_assist(
        db_session,
        grant_read=grant_read,
        version_state=version_state,
    )
    provider = ContextClaimProvider(unsupported=unsupported)
    before = await _outbox_count(db_session)
    client = await _staff_client(db_session, provider)
    try:
        response = await client.post(
            "/api/v1/staff/knowledge/search",
            json={"question": "refund"},
            cookies={"staff_session": cookie},
        )
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.json()["refused"] is True
    assert response.json()["citations"] == []
    assert await _outbox_count(db_session) == before
    assert provider.calls == int(grant_read and version_state is DocumentVersionState.RETRIEVABLE)
