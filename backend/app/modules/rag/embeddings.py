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
    ) -> DocumentVersion:
        version = await self._db_session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.id == version_id)
            .with_for_update()
        )
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
                    .order_by(DocumentChunk.ordinal)
                    .with_for_update()
                )
            ).all()
        )
        if not chunks:
            raise ValueError("a document version must contain chunks before publication")
        vectors = await self._provider.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks) or not all(_valid_vector(vector) for vector in vectors):
            raise ValueError("embedding provider returned an invalid vector batch")
        if before_publish is not None:
            await before_publish()
        for chunk, vector in zip(chunks, vectors, strict=True):
            chunk.embedding = vector
        document = await self._db_session.get(Document, version.document_id, with_for_update=True)
        if document is None:
            raise LookupError("document not found")
        # The embeddings, RETRIEVABLE state, and current-version switch are all
        # flushed together.  The caller owns the transaction commit boundary.
        version.state = DocumentVersionState.RETRIEVABLE
        document.current_version_id = version.id
        await self._db_session.flush()
        return version


def _valid_vector(vector: Sequence[float]) -> bool:
    return len(vector) == EMBEDDING_DIMENSIONS and all(math.isfinite(value) for value in vector)
