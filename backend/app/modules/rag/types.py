from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    title: str = ""
    internal_drive_link: str | None = None
    retrieval_eligible: bool = True


class AnswerAudience(StrEnum):
    CUSTOMER = "CUSTOMER"
    STAFF = "STAFF"


class CustomerCitation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    section: str | None
    page_number: int | None


class SourceCitation(BaseModel):
    """A citation whose detailed projection is restricted to staff callers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: UUID
    document_version_id: UUID
    title: str
    section: str | None
    page_number: int | None
    internal_drive_link: str | None = None

    def for_audience(self, audience: AnswerAudience) -> CustomerCitation | SourceCitation:
        if audience is AnswerAudience.CUSTOMER:
            return CustomerCitation(
                title=self.title,
                section=self.section,
                page_number=self.page_number,
            )
        return self


class ClaimSupport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1, max_length=4_000)
    citation_ids: list[UUID] = Field(default_factory=list)
    material: bool = True


class ValidatedAnswer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    claims: list[ClaimSupport]
    citations: list[CustomerCitation | SourceCitation]
    segments: list[str]
    refused: bool
    model: str
    prompt_version: str
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)


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
