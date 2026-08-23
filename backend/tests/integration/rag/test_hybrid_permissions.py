from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.identity.models import Organization, StaffSession, StaffUser, UserRole, UserStatus
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    KnowledgeBase,
)
from app.modules.rag.text_search import TextCandidateSource
from app.modules.rag.vector_search import VectorCandidateSource


async def _principal(db_session, organization: Organization) -> Principal:  # type: ignore[no-untyped-def]
    user = StaffUser(
        organization_id=organization.id,
        oidc_subject=f"rag-{uuid4()}",
        email="member@example.test",
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    db_session.add(user)
    await db_session.flush()
    session = StaffSession(
        user_id=user.id,
        csrf_hash="test",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    db_session.add(session)
    await db_session.flush()
    return Principal(user.id, organization.id, user.email, user.role, session.id, "test")


async def _retrievable_chunk(  # type: ignore[no-untyped-def]
    db_session, organization: Organization, *, text: str, embedding: list[float]
) -> tuple[KnowledgeBase, DocumentChunk]:
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
        title="Policy",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.RETRIEVABLE,
        content_sha256=uuid4().hex + uuid4().hex,
    )
    db_session.add(version)
    await db_session.flush()
    document.current_version_id = version.id
    chunk = DocumentChunk(
        id=uuid4(),
        document_version_id=version.id,
        ordinal=0,
        text=text,
        page_number=1,
        section="Policy",
        token_count=2,
        metadata_={},
        embedding=embedding,
    )
    db_session.add(chunk)
    await db_session.flush()
    return knowledge_base, chunk


async def _grant_read(db_session, principal: Principal, knowledge_base_id: UUID) -> None:
    db_session.add(
        ResourceGrant(
            organization_id=principal.organization_id,
            subject_id=principal.subject_id,
            resource_type="knowledge",
            resource_id=knowledge_base_id,
            actions=["knowledge.read"],
        )
    )
    await db_session.flush()


@pytest.mark.asyncio
async def test_each_candidate_branch_excludes_unauthorized_chunks(db_session) -> None:
    organization = Organization(name=f"RAG org {uuid4()}")
    foreign_organization = Organization(name=f"RAG foreign {uuid4()}")
    db_session.add_all([organization, foreign_organization])
    await db_session.flush()
    principal = await _principal(db_session, organization)
    authorized_knowledge_base, authorized_chunk = await _retrievable_chunk(
        db_session, organization, text="policy refund", embedding=[1.0] * 1536
    )
    await _retrievable_chunk(
        db_session, foreign_organization, text="policy foreign", embedding=[1.0] * 1536
    )
    await _grant_read(db_session, principal, authorized_knowledge_base.id)
    await db_session.flush()

    vector = await VectorCandidateSource(db_session).search(
        principal, authorized_knowledge_base.id, "policy", 10, query_embedding=[1.0] * 1536
    )
    text = await TextCandidateSource(db_session).search(
        principal, authorized_knowledge_base.id, "policy", 10
    )

    assert {item.chunk_id for item in vector} == {authorized_chunk.id}
    assert {item.chunk_id for item in text} == {authorized_chunk.id}
    assert all(item.organization_id == principal.organization_id for item in vector + text)
    assert all(item.resource_authorized for item in vector + text)


@pytest.mark.asyncio
async def test_revoked_current_version_is_excluded_before_each_branch_ranks(db_session) -> None:
    organization = Organization(name=f"RAG revoked {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
    principal = await _principal(db_session, organization)
    knowledge_base, chunk = await _retrievable_chunk(
        db_session, organization, text="policy refund", embedding=[1.0] * 1536
    )
    await _grant_read(db_session, principal, knowledge_base.id)
    version = await db_session.get(DocumentVersion, chunk.document_version_id)
    assert version is not None
    version.state = DocumentVersionState.REVOKED
    await db_session.flush()

    vector = await VectorCandidateSource(db_session).search(
        principal, knowledge_base.id, "policy", 10, query_embedding=[1.0] * 1536
    )
    text = await TextCandidateSource(db_session).search(principal, knowledge_base.id, "policy", 10)

    assert vector == []
    assert text == []
