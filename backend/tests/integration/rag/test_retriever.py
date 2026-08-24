from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
)
from app.modules.rag.embeddings import EmbeddingPublicationService
from app.modules.rag.retriever import HybridRetriever
from app.modules.rag.types import RetrievedChunk


class FakeEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1536 for _ in texts]


@pytest.mark.asyncio
async def test_embedding_publication_switches_current_version_only_after_every_chunk_is_valid(
    db_session,
) -> None:
    from app.modules.identity.models import Organization
    from app.modules.knowledge.models import DriveSource, KnowledgeBase

    organization = Organization(name=f"embedding {uuid4()}")
    db_session.add(organization)
    await db_session.flush()
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
        external_id="document",
        title="Policy",
        mime_type="application/pdf",
    )
    db_session.add(document)
    await db_session.flush()
    version = DocumentVersion(
        document_id=document.id,
        state=DocumentVersionState.PROCESSING,
        content_sha256="a" * 64,
    )
    db_session.add(version)
    await db_session.flush()
    db_session.add_all(
        [
            DocumentChunk(
                id=uuid4(), document_version_id=version.id, ordinal=index, text=f"policy {index}",
                page_number=None, section=None, token_count=2, metadata_={}
            )
            for index in range(2)
        ]
    )
    await db_session.flush()

    published = await EmbeddingPublicationService(db_session, FakeEmbeddingProvider()).publish(
        version.id
    )

    assert published.state is DocumentVersionState.RETRIEVABLE
    assert document.current_version_id == version.id
    from sqlalchemy import select

    chunks = list(
        (
            await db_session.scalars(
                select(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
            )
        ).all()
    )
    assert all(chunk.embedding is not None for chunk in chunks)


@pytest.mark.asyncio
async def test_hybrid_retriever_runs_both_branches_and_fuses_stable_chunk_ids() -> None:
    class VectorSource:
        async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return [chunk("a"), chunk("b")]

    class TextSource:
        async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return [chunk("b"), chunk("c")]

    class Provider:
        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0] * 1536]

    result = await HybridRetriever(VectorSource(), TextSource(), Provider()).retrieve(
        object(), uuid4(), "policy", 10
    )

    assert [item.stable_id for item in result] == ["b", "a", "c"]


@pytest.mark.asyncio
async def test_hybrid_retriever_uses_independent_postgresql_sessions_for_parallel_branches(
) -> None:
    from app.core.database import async_sessionmaker, engine
    from app.modules.authorization.models import ResourceGrant
    from app.modules.identity.dependencies import Principal
    from app.modules.identity.models import (
        Organization,
        StaffSession,
        StaffUser,
        UserRole,
        UserStatus,
    )
    from app.modules.knowledge.models import DriveSource, KnowledgeBase

    async with async_sessionmaker() as db_session:
        organization = Organization(name=f"parallel hybrid {uuid4()}")
        db_session.add(organization)
        await db_session.flush()
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
        user = StaffUser(
            organization_id=organization.id,
            oidc_subject=f"parallel-{uuid4()}",
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
        db_session.add(
            ResourceGrant(
                organization_id=organization.id,
                subject_id=user.id,
                resource_type="knowledge",
                resource_id=knowledge_base.id,
                actions=["knowledge.read"],
            )
        )
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
        db_session.add(
            DocumentChunk(
                id=uuid4(),
                document_version_id=version.id,
                ordinal=0,
                text="policy refund",
                page_number=1,
                section="Policy",
                token_count=2,
                metadata_={},
                embedding=[1.0] * 1536,
            )
        )
        await db_session.commit()

        principal = Principal(user.id, organization.id, user.email, user.role, session.id, "test")
        document_id = document.id
        knowledge_base_id = knowledge_base.id
        organization_id = organization.id

    try:
        result = await HybridRetriever.from_session_factory(
            async_sessionmaker,
            FakeEmbeddingProvider(),
        ).retrieve(principal, knowledge_base_id, "policy", 10)

        assert [item.document_id for item in result] == [document_id]
    finally:
        async with async_sessionmaker() as cleanup_session:
            await cleanup_session.execute(
                delete(Organization).where(Organization.id == organization_id)
            )
            await cleanup_session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_hybrid_retriever_rejects_shared_postgresql_session(db_session) -> None:  # type: ignore[no-untyped-def]
    from app.modules.rag.text_search import TextCandidateSource
    from app.modules.rag.vector_search import VectorCandidateSource

    retriever = HybridRetriever(
        VectorCandidateSource(db_session),
        TextCandidateSource(db_session),
        FakeEmbeddingProvider(),
    )

    with pytest.raises(RuntimeError, match="independently scoped database sessions"):
        await retriever.retrieve(object(), uuid4(), "policy", 10)


def chunk(stable_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(), stable_id=stable_id, document_version_id=uuid4(), document_id=uuid4(),
        organization_id=uuid4(), knowledge_base_id=uuid4(), ordinal=0, text=stable_id,
        page_number=None, section=None, resource_authorized=True,
    )
