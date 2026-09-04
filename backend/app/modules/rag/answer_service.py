import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.core.config import Settings
from app.core.telemetry import (
    record_grounded_answer,
    record_model_latency,
    record_retrieval_latency,
)
from app.modules.identity.dependencies import Principal
from app.modules.rag.citations import citation_from_chunk, project_citations
from app.modules.rag.groundedness import CitationValidator, GroundednessError
from app.modules.rag.llm import (
    GeneratedAnswer,
    GenerationProvider,
    ProviderCircuitBreaker,
    ProviderTransientError,
)
from app.modules.rag.prompts import PROMPT_VERSION, build_grounded_prompt
from app.modules.rag.types import (
    AnswerAudience,
    RetrievedChunk,
    Retriever,
    SourceCitation,
    ValidatedAnswer,
)

_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_DEFAULT_REFUSAL = (
    "I don't know based on the available information. Please contact a team member for help."
)


@dataclass(frozen=True, slots=True)
class AnswerExecution:
    answer: ValidatedAnswer
    retrieved_chunks: list[RetrievedChunk]
    retrieval_latency_ms: int
    model_latency_ms: int
    source_citations: list[SourceCitation] = field(default_factory=list)


class GroundedAnswerService:
    """Retrieve, generate, validate, then expose only a safe complete answer."""

    def __init__(
        self,
        retriever: Retriever,
        provider: GenerationProvider,
        validator: CitationValidator,
        circuit_breaker: ProviderCircuitBreaker,
        *,
        refusal_message: str = _DEFAULT_REFUSAL,
        retrieval_limit: int = 8,
        telemetry: Callable[..., None] = record_grounded_answer,
    ) -> None:
        self._retriever = retriever
        self._provider = provider
        self._validator = validator
        self._circuit_breaker = circuit_breaker
        self._refusal_message = refusal_message
        self._retrieval_limit = retrieval_limit
        self._telemetry = telemetry

    @classmethod
    def from_settings(cls, settings: "Settings") -> "GroundedAnswerService":
        """Build the live read-only RAG path from explicitly configured providers."""
        from redis.asyncio import from_url

        from app.core.database import async_sessionmaker
        from app.modules.rag.embeddings import OpenAIEmbeddingProvider
        from app.modules.rag.llm import AnthropicGenerationProvider, RedisCircuitStore
        from app.modules.rag.retriever import HybridRetriever

        if (
            settings.openai_api_key is None
            or settings.anthropic_api_key is None
            or settings.redis_url is None
        ):
            raise RuntimeError(
                "OPENAI_API_KEY, ANTHROPIC_API_KEY, and REDIS_URL are required for Staff Assist"
            )
        embedding_provider = OpenAIEmbeddingProvider.from_settings(settings)
        retriever = HybridRetriever.from_session_factory(
            async_sessionmaker,
            embedding_provider,
            reranker_enabled=settings.reranker_enabled,
        )
        provider = AnthropicGenerationProvider(
            settings.anthropic_api_key.get_secret_value(),
            model=settings.anthropic_model,
            base_url=(
                settings.anthropic_base_url.unicode_string()
                if settings.anthropic_base_url is not None
                else None
            ),
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
            provider,
            CitationValidator(),
            circuit_breaker,
            refusal_message=settings.grounded_refusal_message,
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
        started = time.monotonic()
        retrieval_started = time.monotonic()
        chunks = await self._retriever.retrieve(
            principal, knowledge_base_id, query, self._retrieval_limit
        )
        retrieval_latency_ms = _latency_ms(retrieval_started)
        record_retrieval_latency(retrieval_latency_ms)
        prompt = build_grounded_prompt(query, chunks)
        provider_name = "claude"
        if not chunks or not await self._circuit_breaker.allow(provider_name):
            return AnswerExecution(
                self._refusal(audience, started, provider_name, "refused", len(chunks)),
                chunks,
                retrieval_latency_ms,
                0,
            )
        model_started = time.monotonic()
        try:
            generation = await self._provider.generate(prompt)
        except ProviderTransientError:
            await self._circuit_breaker.record_transient_failure(provider_name)
            model_latency_ms = _latency_ms(model_started)
            record_model_latency(model_latency_ms)
            return AnswerExecution(
                self._refusal(audience, started, provider_name, "provider_error", len(chunks)),
                chunks,
                retrieval_latency_ms,
                model_latency_ms,
            )
        except Exception:
            model_latency_ms = _latency_ms(model_started)
            record_model_latency(model_latency_ms)
            return AnswerExecution(
                self._refusal(audience, started, provider_name, "provider_error", len(chunks)),
                chunks,
                retrieval_latency_ms,
                model_latency_ms,
            )
        model_latency_ms = _latency_ms(model_started)
        record_model_latency(model_latency_ms)
        if not isinstance(generation, GeneratedAnswer):
            return AnswerExecution(
                self._refusal(audience, started, provider_name, "provider_error", len(chunks)),
                chunks,
                retrieval_latency_ms,
                model_latency_ms,
            )
        try:
            citations = self._validator.validate(generation, chunks, principal, knowledge_base_id)
        except (GroundednessError, ValueError):
            return AnswerExecution(
                self._refusal(audience, started, provider_name, "validation_refusal", len(chunks)),
                chunks,
                retrieval_latency_ms,
                model_latency_ms,
            )
        await self._circuit_breaker.record_success(provider_name)
        latency_ms = _latency_ms(started)
        estimated_cost = _estimated_cost(generation.input_tokens, generation.output_tokens)
        answer = ValidatedAnswer(
            text=generation.text,
            claims=generation.claims,
            citations=project_citations(citations, audience),
            segments=_segments(generation.text),
            refused=False,
            model=generation.model,
            prompt_version=prompt.version,
            latency_ms=latency_ms,
            input_tokens=generation.input_tokens,
            output_tokens=generation.output_tokens,
            estimated_cost=estimated_cost,
        )
        self._record(audience, generation.model, "validated", answer, len(chunks))
        return AnswerExecution(
            answer,
            chunks,
            retrieval_latency_ms,
            model_latency_ms,
            source_citations=[citation_from_chunk(chunk) for chunk in citations],
        )

    def _refusal(
        self,
        audience: AnswerAudience,
        started: float,
        model: str,
        outcome: str,
        retrieved_chunk_count: int,
    ) -> ValidatedAnswer:
        answer = ValidatedAnswer(
            text=self._refusal_message,
            claims=[],
            citations=[],
            segments=_segments(self._refusal_message),
            refused=True,
            model=model,
            prompt_version=PROMPT_VERSION,
            latency_ms=_latency_ms(started),
            input_tokens=0,
            output_tokens=0,
            estimated_cost=0.0,
        )
        self._record(audience, model, outcome, answer, retrieved_chunk_count)
        return answer

    def _record(
        self,
        audience: AnswerAudience,
        model: str,
        outcome: str,
        answer: ValidatedAnswer,
        retrieved_chunk_count: int,
    ) -> None:
        self._telemetry(
            audience=audience.value,
            model=model,
            prompt_version=answer.prompt_version,
            outcome=outcome,
            retrieved_chunk_count=retrieved_chunk_count,
            latency_ms=answer.latency_ms,
            input_tokens=answer.input_tokens,
            output_tokens=answer.output_tokens,
            estimated_cost=answer.estimated_cost,
        )


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


def _estimated_cost(input_tokens: int, output_tokens: int) -> float:
    """Conservative configurable-model baseline: $3/M input and $15/M output."""

    return (input_tokens * 3 + output_tokens * 15) / 1_000_000


def _segments(text: str) -> list[str]:
    return [segment for segment in _SENTENCES.split(text.strip()) if segment]
