from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.identity.dependencies import Principal


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A candidate that has already passed every retrieval authorization predicate."""

    chunk_id: UUID
    stable_id: str
    document_version_id: UUID
    document_id: UUID
    organization_id: UUID
    knowledge_base_id: UUID
    ordinal: int
    text: str
    page_number: int | None
    section: str | None
    resource_authorized: bool


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorCandidateSource(Protocol):
    async def search(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
        *,
        query_embedding: list[float] | None = None,
    ) -> list[RetrievedChunk]: ...


class TextCandidateSource(Protocol):
    async def search(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]: ...


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: list[RetrievedChunk]
    ) -> list[RetrievedChunk]: ...


class Retriever(Protocol):
    async def retrieve(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]: ...
