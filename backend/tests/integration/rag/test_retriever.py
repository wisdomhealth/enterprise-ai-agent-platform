from uuid import uuid4

import pytest

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


def chunk(stable_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=uuid4(), stable_id=stable_id, document_version_id=uuid4(), document_id=uuid4(),
        organization_id=uuid4(), knowledge_base_id=uuid4(), ordinal=0, text=stable_id,
        page_number=None, section=None, resource_authorized=True,
    )
