import math
from collections.abc import Sequence
from typing import Protocol, cast
from uuid import UUID

from openai import AsyncOpenAI
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


class _EmbeddingResponseItem(Protocol):
    index: int
    embedding: Sequence[float]


class _EmbeddingResponse(Protocol):
    data: Sequence[_EmbeddingResponseItem]


class _EmbeddingsAPI(Protocol):
    async def create(
        self, *, input: list[str], model: str, dimensions: int
    ) -> _EmbeddingResponse: ...


class _OpenAIClient(Protocol):
    embeddings: _EmbeddingsAPI


class OpenAIEmbeddingProvider:
    """Small adapter boundary for the configured OpenAI embedding model."""

    def __init__(self, client: object, *, model: str = EMBEDDING_MODEL) -> None:
        self._client = cast(_OpenAIClient, client)
        self._model = model

    @classmethod
    def from_settings(cls, settings: Settings) -> "OpenAIEmbeddingProvider":
        if settings.openai_api_key is None:
            raise RuntimeError("OPENAI_API_KEY is required for embeddings")
        return cls(AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value()))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = await self._client.embeddings.create(
            input=texts,
            model=self._model,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        vectors: list[list[float] | None] = [None] * len(texts)
        for item in response.data:
            if item.index < 0 or item.index >= len(texts):
                raise ValueError("embedding response index is outside the submitted batch")
            vectors[item.index] = [float(value) for value in item.embedding]
        if any(vector is None for vector in vectors):
            raise ValueError("embedding response does not contain every submitted text")
        return [vector for vector in vectors if vector is not None]


class EmbeddingPublicationService:
    """Persist a complete embedding set before publishing a new document version."""

    def __init__(self, db_session: AsyncSession, provider: EmbeddingProvider) -> None:
        self._db_session = db_session
        self._provider = provider

    async def publish(self, version_id: UUID) -> DocumentVersion:
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
