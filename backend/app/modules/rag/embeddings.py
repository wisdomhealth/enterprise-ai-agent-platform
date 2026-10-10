import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, cast
from uuid import UUID

from llama_index.embeddings.openai import OpenAIEmbedding  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.modules.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentVersion,
    DocumentVersionState,
)
from app.modules.rag.types import EmbeddingProvider

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


class _LlamaIndexEmbedding(Protocol):
    async def aget_text_embedding_batch(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingProvider:
    """LlamaIndex OpenAI adapter behind the application's async batch protocol."""

    def __init__(self, embed_model: object, *, dimensions: int = EMBEDDING_DIMENSIONS) -> None:
        self._embed_model = cast(_LlamaIndexEmbedding, embed_model)
        self._dimensions = dimensions

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAIEmbeddingProvider":
        if settings.openai_api_key is None:
            raise RuntimeError("OPENAI_API_KEY is required for embeddings")
        kwargs: dict[str, object] = {
            "api_key": settings.openai_api_key.get_secret_value(),
            "model": settings.openai_embedding_model,
            "dimensions": settings.openai_embedding_dimensions,
            "embed_batch_size": settings.openai_embedding_batch_size,
            "max_retries": settings.openai_embedding_max_retries,
            "timeout": settings.openai_request_timeout_seconds,
        }
        if settings.openai_base_url is not None:
            kwargs["api_base"] = settings.openai_base_url.unicode_string().rstrip("/")
        return cls(
            OpenAIEmbedding(**kwargs),
            dimensions=settings.openai_embedding_dimensions,
        )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        raw_vectors = await self._embed_model.aget_text_embedding_batch(texts)
        vectors = [[float(value) for value in vector] for vector in raw_vectors]
        if len(vectors) != len(texts) or not all(
            len(vector) == self._dimensions and all(math.isfinite(value) for value in vector)
            for vector in vectors
        ):
            raise ValueError("embedding provider returned an invalid vector batch")
        return vectors


class EmbeddingPublicationService:
    """Persist a complete embedding set before publishing a new document version."""

    def __init__(self, db_session: AsyncSession, provider: EmbeddingProvider) -> None:
        self._db_session = db_session
        self._provider = provider

    async def publish(
        self,
        version_id: UUID,
        *,
        before_publish: Callable[[], Awaitable[None]] | None = None,
        transition: Callable[
            [Document, DocumentVersion, list[DocumentVersion], list[DocumentChunk]],
            Awaitable[None],
        ]
        | None = None,
    ) -> DocumentVersion:
        # Snapshot the texts without write locks. Embedding is external I/O and
        # must not keep version/chunk locks while waiting on the provider.
        version = await self._db_session.get(DocumentVersion, version_id)
        if version is None:
            raise LookupError("document version not found")
        if version.state is DocumentVersionState.RETRIEVABLE:
            return version
        if version.state is not DocumentVersionState.PROCESSING:
            raise ValueError("only processing document versions can be published")
        chunks = list(
            (
                await self._db_session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_version_id == version.id)
                    .order_by(DocumentChunk.ordinal, DocumentChunk.id)
                )
            ).all()
        )
        if not chunks:
            raise ValueError("a document version must contain chunks before publication")
        document_id = version.document_id
        chunk_snapshot = tuple((chunk.id, chunk.ordinal, chunk.text) for chunk in chunks)
        # End the clean snapshot read transaction before calling the external
        # embedding provider. The durable parse checkpoint is required here;
        # committing pending caller writes would break the publication fence.
        if self._db_session.new or self._db_session.dirty or self._db_session.deleted:
            raise RuntimeError("embedding publication requires a durable parse checkpoint")
        await self._db_session.commit()
        vectors = await self._provider.embed([item[2] for item in chunk_snapshot])
        if len(vectors) != len(chunks) or not all(_valid_vector(vector) for vector in vectors):
            raise ValueError("embedding provider returned an invalid vector batch")
        if before_publish is not None:
            await before_publish()
        # Standalone callers use Document -> DocumentVersion -> DocumentChunk.
        # Drive ingestion's callback first holds Source -> ordered JobIntent rows;
        # it then locks this Document before the ordered versions and chunks below.
        document = await self._db_session.get(Document, document_id, with_for_update=True)
        if document is None:
            raise LookupError("document not found")
        locked_versions = list(
            (
                await self._db_session.scalars(
                    select(DocumentVersion)
                    .where(DocumentVersion.document_id == document.id)
                    .order_by(DocumentVersion.id)
                    .with_for_update()
                )
            ).all()
        )
        version = next(
            (item for item in locked_versions if item.id == version_id),
            None,
        )
        if version is None:
            raise LookupError("document version not found")
        if version.state is DocumentVersionState.RETRIEVABLE:
            return version
        if version.state is not DocumentVersionState.PROCESSING:
            raise ValueError("only processing document versions can be published")
        relevant_version_ids = {version.id}
        if document.current_version_id is not None:
            relevant_version_ids.add(document.current_version_id)
        locked_chunks = list(
            (
                await self._db_session.scalars(
                    select(DocumentChunk)
                    .where(DocumentChunk.document_version_id.in_(relevant_version_ids))
                    .order_by(DocumentChunk.document_version_id, DocumentChunk.id)
                    .with_for_update()
                )
            ).all()
        )
        target_chunks = [
            chunk for chunk in locked_chunks if chunk.document_version_id == version.id
        ]
        if tuple(
            (chunk.id, chunk.ordinal, chunk.text)
            for chunk in sorted(target_chunks, key=lambda item: (item.ordinal, item.id))
        ) != chunk_snapshot:
            raise ValueError("document chunks changed during embedding")
        for chunk, vector in zip(
            sorted(target_chunks, key=lambda item: (item.ordinal, item.id)),
            vectors,
            strict=True,
        ):
            chunk.embedding = vector
        # The embeddings, RETRIEVABLE state, and current-version switch are all
        # flushed together.  The caller owns the transaction commit boundary.
        version.state = DocumentVersionState.RETRIEVABLE
        if transition is None:
            document.current_version_id = version.id
        else:
            await transition(document, version, locked_versions, locked_chunks)
        await self._db_session.flush()
        return version


def _valid_vector(vector: Sequence[float]) -> bool:
    return len(vector) == EMBEDDING_DIMENSIONS and all(math.isfinite(value) for value in vector)
