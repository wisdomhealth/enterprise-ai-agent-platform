from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.telemetry import (
    record_grounded_answer,
    record_model_latency,
    record_retrieval_latency,
)
from app.modules.identity.dependencies import Principal
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import GenerationProvider, ProviderCircuitBreaker
from app.modules.rag.types import (
    AnswerAudience,
    RetrievedChunk,
    Retriever,
    SourceCitation,
    ValidatedAnswer,
)
from app.modules.rag.workflow import AnswerWorkflowRequest, GroundedAnswerWorkflow


@dataclass(frozen=True, slots=True)
class AnswerExecution:
    answer: ValidatedAnswer
    retrieved_chunks: list[RetrievedChunk]
    retrieval_latency_ms: int
    model_latency_ms: int
    source_citations: list[SourceCitation] = field(default_factory=list)


class GroundedAnswerService:
    """Stable service facade over the bounded LangGraph answer workflow."""

    def __init__(
        self,
        retriever: Retriever,
        provider: GenerationProvider,
        validator: CitationValidator,
        circuit_breaker: ProviderCircuitBreaker,
        *,
        refusal_message: str = (
            "I don't know based on the available information. "
            "Please contact a team member for help."
        ),
        retrieval_limit: int = 8,
        max_attempts: int = 2,
        total_timeout_seconds: float = 60.0,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
        telemetry: Callable[..., None] = record_grounded_answer,
    ) -> None:
        self._workflow = GroundedAnswerWorkflow(
            retriever,
            provider,
            validator,
            circuit_breaker,
            refusal_message=refusal_message,
            retrieval_limit=retrieval_limit,
            max_attempts=max_attempts,
            total_timeout_seconds=total_timeout_seconds,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
        )
        self._telemetry = telemetry

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> GroundedAnswerService:
        """Build the live OpenAI read path with authorized LlamaIndex retrieval."""
        from redis.asyncio import from_url

        from app.modules.rag.embeddings import OpenAIEmbeddingProvider
        from app.modules.rag.llm import OpenAIGenerationProvider, RedisCircuitStore
        from app.modules.rag.retriever import HybridRetriever

        if settings.openai_api_key is None or settings.redis_url is None:
            raise RuntimeError("OPENAI_API_KEY and REDIS_URL are required for Staff Assist")
        if session_factory is None:
            from app.core.database import async_sessionmaker as request_sessionmaker

            session_factory = request_sessionmaker
        retriever = HybridRetriever.from_session_factory(
            session_factory,
            OpenAIEmbeddingProvider.from_settings(settings),
            reranker_enabled=settings.reranker_enabled,
        )
        circuit_breaker = ProviderCircuitBreaker(
            RedisCircuitStore(
                from_url(str(settings.redis_url))  # type: ignore[no-untyped-call]
            ),
            failure_threshold=settings.provider_circuit_failure_threshold,
            reset_seconds=settings.provider_circuit_reset_seconds,
        )
        return cls(
            retriever,
            OpenAIGenerationProvider.from_settings(settings),
            CitationValidator(),
            circuit_breaker,
            refusal_message=settings.grounded_refusal_message,
            max_attempts=settings.rag_max_generation_attempts,
            total_timeout_seconds=settings.rag_execution_timeout_seconds,
            input_cost_per_million=settings.openai_input_cost_per_million,
            output_cost_per_million=settings.openai_output_cost_per_million,
        )

    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer:
        return (
            await self.answer_with_evidence(principal, knowledge_base_id, query, audience)
        ).answer

    async def answer_with_evidence(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> AnswerExecution:
        execution = await self._workflow.run(
            AnswerWorkflowRequest(
                principal=principal,
                knowledge_base_id=knowledge_base_id,
                query=query,
                audience=audience,
            )
        )
        record_retrieval_latency(execution.retrieval_latency_ms)
        record_model_latency(execution.model_latency_ms)
        answer = execution.answer
        self._telemetry(
            audience=audience.value,
            model=answer.model,
            prompt_version=answer.prompt_version,
            outcome=execution.outcome,
            retrieved_chunk_count=len(execution.retrieved_chunks),
            latency_ms=answer.latency_ms,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            estimated_cost=answer.estimated_cost,
            usage_complete=answer.usage_complete,
        )
        return AnswerExecution(
            answer=answer,
            retrieved_chunks=execution.retrieved_chunks,
            retrieval_latency_ms=execution.retrieval_latency_ms,
            model_latency_ms=execution.model_latency_ms,
            source_citations=execution.source_citations,
        )


def estimated_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    input_rate: float,
    output_rate: float,
) -> float:
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
