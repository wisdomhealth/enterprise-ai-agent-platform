from typing import cast
from uuid import UUID

from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.authorization.models import ResourceGrant
from app.modules.identity.dependencies import Principal
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
    DriveSource,
    DriveSourceStatus,
    KnowledgeBase,
)
from app.modules.rag.types import EmbeddingProvider, RetrievedChunk


class VectorCandidateSource:
    """pgvector branch with authorization predicates applied before ranking."""

    def __init__(
        self,
        db_session: AsyncSession,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._db_session = db_session
        self._embedding_provider = embedding_provider

    async def search(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
        *,
        query_embedding: list[float] | None = None,
    ) -> list[RetrievedChunk]:
        if limit < 1:
            return []
        if query_embedding is None:
            if self._embedding_provider is None:
                raise RuntimeError("an embedding provider is required for vector search")
            vectors = await self._embedding_provider.embed([query])
            if len(vectors) != 1:
                raise ValueError("embedding provider did not return one query vector")
            query_embedding = vectors[0]
        rows = await self._db_session.execute(
            _authorized_chunks_query(principal, knowledge_base_id)
            .where(DocumentChunk.embedding.is_not(None))
            .order_by(DocumentChunk.embedding.cosine_distance(query_embedding), DocumentChunk.id)
            .limit(limit)
        )
        return [
            _candidate(cast(tuple[DocumentChunk, DocumentVersion, Document], tuple(row)))
            for row in rows.all()
        ]


def _authorized_chunks_query(
    principal: Principal, knowledge_base_id: UUID
) -> Select[tuple[DocumentChunk, DocumentVersion, Document]]:
    return (
        select(DocumentChunk, DocumentVersion, Document)
        .join(DocumentVersion, DocumentVersion.id == DocumentChunk.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(DriveSource, DriveSource.id == Document.source_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.knowledge_base_id)
        .join(
            ResourceGrant,
            and_(
                ResourceGrant.organization_id == principal.organization_id,
                ResourceGrant.subject_id == principal.subject_id,
                ResourceGrant.resource_type == "knowledge",
                ResourceGrant.resource_id == KnowledgeBase.id,
                ResourceGrant.actions.contains(["knowledge.read"]),
            ),
        )
        .where(
            Document.organization_id == principal.organization_id,
            Document.knowledge_base_id == knowledge_base_id,
            KnowledgeBase.organization_id == principal.organization_id,
            DocumentVersion.state == DocumentVersionState.RETRIEVABLE,
            Document.current_version_id == DocumentVersion.id,
            DriveSource.organization_id == principal.organization_id,
            DriveSource.knowledge_base_id == knowledge_base_id,
            DriveSource.status == DriveSourceStatus.ACTIVE,
        )
    )


def _candidate(row: tuple[DocumentChunk, DocumentVersion, Document]) -> RetrievedChunk:
    chunk, version, document = row
    return RetrievedChunk(
        chunk_id=chunk.id,
        stable_id=str(chunk.id),
        document_version_id=version.id,
        document_id=document.id,
        organization_id=document.organization_id,
        knowledge_base_id=document.knowledge_base_id,
        ordinal=chunk.ordinal,
        text=chunk.text,
        page_number=chunk.page_number,
        section=chunk.section,
        resource_authorized=True,
    )
