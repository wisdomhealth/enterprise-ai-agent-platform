import re
import time
from collections.abc import Callable
from uuid import UUID

from app.core.telemetry import record_grounded_answer
from app.modules.identity.dependencies import Principal
from app.modules.rag.citations import project_citations
from app.modules.rag.groundedness import CitationValidator, GroundednessError
from app.modules.rag.llm import GenerationProvider, ProviderCircuitBreaker, ProviderTransientError
from app.modules.rag.prompts import PROMPT_VERSION, build_grounded_prompt
from app.modules.rag.types import AnswerAudience, Retriever, ValidatedAnswer

_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_DEFAULT_REFUSAL = (
    "I don't know based on the available information. Please contact a team member for help."
)


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

    async def answer(
        self,
        principal: Principal,
        knowledge_base_id: UUID,
        query: str,
        audience: AnswerAudience,
    ) -> ValidatedAnswer:
        started = time.monotonic()
        chunks = await self._retriever.retrieve(
            principal, knowledge_base_id, query, self._retrieval_limit
        )
        prompt = build_grounded_prompt(query, chunks)
        provider_name = "claude"
        if not chunks or not await self._circuit_breaker.allow(provider_name):
            return self._refusal(audience, started, provider_name, "refused", len(chunks))
        try:
            generation = await self._provider.generate(prompt)
            citations = self._validator.validate(generation, chunks, principal, knowledge_base_id)
        except ProviderTransientError:
            await self._circuit_breaker.record_transient_failure(provider_name)
            return self._refusal(audience, started, provider_name, "provider_error", len(chunks))
        except (GroundednessError, ValueError):
            return self._refusal(
                audience, started, provider_name, "validation_refusal", len(chunks)
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
        return answer

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
