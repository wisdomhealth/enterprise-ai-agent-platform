from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol
from uuid import UUID

from llama_index.core.base.base_retriever import BaseRetriever
from llama_index.core.llms import MockLLM
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.retrievers.fusion_retriever import FUSION_MODES
from llama_index.core.schema import MetadataMode, NodeWithScore, QueryBundle, TextNode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.identity.dependencies import Principal
from app.modules.rag.types import (
    EmbeddingProvider,
    Reranker,
    RetrievedChunk,
    Retriever,
    TextCandidateSource,
    VectorCandidateSource,
)


class _NodePostprocessor(Protocol):
    def postprocess_nodes(
        self,
        nodes: list[NodeWithScore],
        *,
        query_bundle: QueryBundle,
    ) -> list[NodeWithScore]: ...


class _AuthorizedBranchRetriever(BaseRetriever):
    def __init__(
        self,
        search: Callable[[str], Awaitable[list[RetrievedChunk]]],
        authorize: Callable[[RetrievedChunk], bool],
        allowed: dict[UUID, RetrievedChunk],
        limit: int,
    ) -> None:
        self._search = search
        self._authorize = authorize
        self._allowed = allowed
        self._limit = limit
        super().__init__()

    async def _aretrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        chunks = await self._search(query_bundle.query_str)
        nodes: list[NodeWithScore] = []
        for rank, chunk in enumerate(chunks[: self._limit]):
            if not self._authorize(chunk):
                continue
            self._allowed[chunk.chunk_id] = chunk
            nodes.append(
                NodeWithScore(
                    node=_chunk_to_node(chunk),
                    score=float(self._limit - rank),
                )
            )
        return nodes

    def _retrieve(self, query_bundle: QueryBundle) -> list[NodeWithScore]:
        raise RuntimeError("authorized retrieval is async-only")


class HybridRetriever(Retriever):
    """Authorized SQL branches composed with LlamaIndex reciprocal-rank fusion."""

    def __init__(
        self,
        vector_source: VectorCandidateSource,
        text_source: TextCandidateSource,
        embedding_provider: EmbeddingProvider,
        *,
        reranker: Reranker | None = None,
        reranker_enabled: bool = False,
        node_postprocessors: Sequence[_NodePostprocessor] = (),
    ) -> None:
        self._vector_source = vector_source
        self._text_source = text_source
        self._embedding_provider = embedding_provider
        self._reranker = reranker
        self._reranker_enabled = reranker_enabled
        self._node_postprocessors = tuple(node_postprocessors)

    @classmethod
    def from_session_factory(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        *,
        reranker: Reranker | None = None,
        reranker_enabled: bool = False,
        node_postprocessors: Sequence[_NodePostprocessor] = (),
    ) -> "HybridRetriever":
        """Build parallel PostgreSQL branches with a fresh session per branch."""
        from app.modules.rag.text_search import (
            TextCandidateSource as PostgreSQLTextCandidateSource,
        )
        from app.modules.rag.vector_search import (
            VectorCandidateSource as PostgreSQLVectorCandidateSource,
        )

        return cls(
            PostgreSQLVectorCandidateSource(session_factory),
            PostgreSQLTextCandidateSource(session_factory),
            embedding_provider,
            reranker=reranker,
            reranker_enabled=reranker_enabled,
            node_postprocessors=node_postprocessors,
        )

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
        vector_session = getattr(self._vector_source, "bound_session", None)
        text_session = getattr(self._text_source, "bound_session", None)
        if vector_session is not None and vector_session is text_session:
            raise RuntimeError(
                "parallel hybrid retrieval requires independently scoped database sessions"
            )
        allowed: dict[UUID, RetrievedChunk] = {}

        def authorized(chunk: RetrievedChunk) -> bool:
            return (
                chunk.resource_authorized is True
                and chunk.retrieval_eligible is True
                and chunk.organization_id == principal.organization_id
                and chunk.knowledge_base_id == knowledge_base_id
            )

        async def vector_search(query_text: str) -> list[RetrievedChunk]:
            return await self._vector_source.search(
                principal,
                knowledge_base_id,
                query_text,
                limit,
                query_embedding=vectors[0],
            )

        async def text_search(query_text: str) -> list[RetrievedChunk]:
            return await self._text_source.search(
                principal, knowledge_base_id, query_text, limit
            )

        branches: list[BaseRetriever] = [
            _AuthorizedBranchRetriever(vector_search, authorized, allowed, limit),
            _AuthorizedBranchRetriever(text_search, authorized, allowed, limit),
        ]
        fusion = QueryFusionRetriever(
            branches,
            llm=MockLLM(max_tokens=1),
            mode=FUSION_MODES.RECIPROCAL_RANK,
            similarity_top_k=limit,
            num_queries=1,
            use_async=True,
        )
        query_bundle = QueryBundle(query)
        fused_nodes = await fusion.aretrieve(query_bundle)
        for postprocessor in self._node_postprocessors:
            fused_nodes = postprocessor.postprocess_nodes(
                fused_nodes,
                query_bundle=query_bundle,
            )
        fused: list[RetrievedChunk] = []
        for node in fused_nodes:
            chunk = _node_to_chunk(node)
            if chunk is None or allowed.get(chunk.chunk_id) != chunk or not authorized(chunk):
                continue
            fused.append(chunk)
        fused = fused[:limit]
        if self._reranker_enabled and self._reranker is not None:
            return (await self._reranker.rerank(query, fused))[:limit]
        return fused


def _chunk_to_node(chunk: RetrievedChunk) -> TextNode:
    return TextNode(
        id_=str(chunk.chunk_id),
        text=chunk.text,
        metadata={
            "chunk_id": str(chunk.chunk_id),
            "stable_id": chunk.stable_id,
            "document_version_id": str(chunk.document_version_id),
            "document_id": str(chunk.document_id),
            "organization_id": str(chunk.organization_id),
            "knowledge_base_id": str(chunk.knowledge_base_id),
            "ordinal": chunk.ordinal,
            "page_number": chunk.page_number,
            "section": chunk.section,
            "resource_authorized": chunk.resource_authorized,
            "title": chunk.title,
            "internal_drive_link": chunk.internal_drive_link,
            "retrieval_eligible": chunk.retrieval_eligible,
        },
    )


def _node_to_chunk(node_with_score: NodeWithScore) -> RetrievedChunk | None:
    node = node_with_score.node
    metadata = node.metadata
    try:
        chunk_id = UUID(_required_string(metadata, "chunk_id"))
        ordinal = metadata["ordinal"]
        page_number = metadata["page_number"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            return None
        if page_number is not None and (
            not isinstance(page_number, int) or isinstance(page_number, bool)
        ):
            return None
        section = _optional_string(metadata, "section")
        internal_drive_link = _optional_string(metadata, "internal_drive_link")
        resource_authorized = metadata["resource_authorized"]
        retrieval_eligible = metadata["retrieval_eligible"]
        if not isinstance(resource_authorized, bool) or not isinstance(
            retrieval_eligible, bool
        ):
            return None
        if node.node_id != str(chunk_id):
            return None
        return RetrievedChunk(
            chunk_id=chunk_id,
            stable_id=_required_string(metadata, "stable_id"),
            document_version_id=UUID(_required_string(metadata, "document_version_id")),
            document_id=UUID(_required_string(metadata, "document_id")),
            organization_id=UUID(_required_string(metadata, "organization_id")),
            knowledge_base_id=UUID(_required_string(metadata, "knowledge_base_id")),
            ordinal=ordinal,
            text=node.get_content(metadata_mode=MetadataMode.NONE),
            page_number=page_number,
            section=section,
            resource_authorized=resource_authorized,
            title=_string(metadata, "title"),
            internal_drive_link=internal_drive_link,
            retrieval_eligible=retrieval_eligible,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _required_string(metadata: dict[str, object], key: str) -> str:
    value = _string(metadata, key)
    if not value:
        raise ValueError(f"{key} is empty")
    return value


def _string(metadata: dict[str, object], key: str) -> str:
    value = metadata[key]
    if not isinstance(value, str):
        raise TypeError(f"{key} is not a string")
    return value


def _optional_string(metadata: dict[str, object], key: str) -> str | None:
    value = metadata[key]
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{key} is not an optional string")
    return value
