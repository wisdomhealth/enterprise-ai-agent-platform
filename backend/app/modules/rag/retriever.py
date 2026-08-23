import asyncio
from uuid import UUID

from app.modules.identity.dependencies import Principal
from app.modules.rag.rrf import reciprocal_rank_fusion
from app.modules.rag.types import (
    EmbeddingProvider,
    Reranker,
    RetrievedChunk,
    Retriever,
    TextCandidateSource,
    VectorCandidateSource,
)


class HybridRetriever(Retriever):
    """Concurrent vector/text retrieval followed by scale-independent RRF."""

    def __init__(
        self,
        vector_source: VectorCandidateSource,
        text_source: TextCandidateSource,
        embedding_provider: EmbeddingProvider,
        *,
        reranker: Reranker | None = None,
        reranker_enabled: bool = False,
    ) -> None:
        self._vector_source = vector_source
        self._text_source = text_source
        self._embedding_provider = embedding_provider
        self._reranker = reranker
        self._reranker_enabled = reranker_enabled

    async def retrieve(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        limit: int,
    ) -> list[RetrievedChunk]:
        if limit < 1:
            return []
        vectors = await self._embedding_provider.embed([query])
        if len(vectors) != 1:
            raise ValueError("embedding provider did not return one query vector")
        vector, text = await asyncio.gather(
            self._vector_source.search(
                principal, knowledge_base_id, query, limit, query_embedding=vectors[0]
            ),
            self._text_source.search(principal, knowledge_base_id, query, limit),
        )
        fused = reciprocal_rank_fusion([vector, text])[:limit]
        if self._reranker_enabled and self._reranker is not None:
            return (await self._reranker.rerank(query, fused))[:limit]
        return fused
