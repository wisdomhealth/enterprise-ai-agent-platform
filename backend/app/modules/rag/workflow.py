from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.modules.identity.dependencies import Principal
from app.modules.rag.citations import citation_from_chunk, project_citations
from app.modules.rag.groundedness import CitationValidator, GroundednessError
from app.modules.rag.llm import (
    GeneratedAnswer,
    GenerationProvider,
    ProviderCircuitBreaker,
    ProviderResponseError,
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
_PROVIDER_NAME = "openai"
_DEFAULT_REFUSAL = (
    "I don't know based on the available information. Please contact a team member for help."
)


@dataclass(frozen=True, slots=True)
class AnswerWorkflowRequest:
    principal: Principal
    knowledge_base_id: UUID
    query: str
    audience: AnswerAudience


@dataclass(frozen=True, slots=True)
class AnswerWorkflowExecution:
    answer: ValidatedAnswer
    retrieved_chunks: list[RetrievedChunk]
    retrieval_latency_ms: int
    model_latency_ms: int
    source_citations: list[SourceCitation] = field(default_factory=list)
    outcome: str = "refused"


@dataclass(frozen=True, slots=True)
class AnswerRuntimeContext:
    retriever: Retriever
    provider: GenerationProvider
    validator: CitationValidator
    circuit_breaker: ProviderCircuitBreaker
    refusal_message: str
    retrieval_limit: int
    max_attempts: int
    input_cost_per_million: float
    output_cost_per_million: float


class AnswerState(TypedDict, total=False):
    request: AnswerWorkflowRequest
    started: float
    chunks: list[RetrievedChunk]
    retrieval_latency_ms: int
    model_latency_ms: int
    attempts: int
    input_tokens: int
    output_tokens: int
    usage_complete: bool
    generation: GeneratedAnswer
    citation_chunks: list[RetrievedChunk]
    route: str
    outcome: str
    execution: AnswerWorkflowExecution


def _context(runtime: Runtime[AnswerRuntimeContext]) -> AnswerRuntimeContext:
    if runtime.context is None:
        raise RuntimeError("answer workflow runtime context is required")
    return runtime.context


async def _validate_scope(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    del runtime
    request = state["request"]
    valid = (
        isinstance(request.principal.organization_id, UUID)
        and isinstance(request.knowledge_base_id, UUID)
        and bool(request.query.strip())
    )
    return {
        "route": "retrieve" if valid else "refuse",
        "outcome": "scope_refusal" if not valid else "pending",
    }


def _route_scope(state: AnswerState) -> str:
    return state["route"]


async def _retrieve(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    context = _context(runtime)
    request = state["request"]
    started = time.monotonic()
    try:
        chunks = await context.retriever.retrieve(
            request.principal,
            request.knowledge_base_id,
            request.query,
            context.retrieval_limit,
        )
    except (PermissionError, ValueError):
        return {
            "chunks": [],
            "retrieval_latency_ms": _latency_ms(started),
            "route": "refuse",
            "outcome": "authorization_refusal",
        }
    except Exception:
        return {
            "chunks": [],
            "retrieval_latency_ms": _latency_ms(started),
            "route": "refuse",
            "outcome": "retrieval_error",
        }
    return {
        "chunks": chunks,
        "retrieval_latency_ms": _latency_ms(started),
        "route": "check_circuit" if chunks else "refuse",
        "outcome": "pending" if chunks else "no_evidence",
    }


def _route_evidence(state: AnswerState) -> str:
    return state["route"]


async def _check_circuit(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    try:
        allowed = await _context(runtime).circuit_breaker.allow(_PROVIDER_NAME)
    except Exception:
        allowed = False
    return {
        "route": "generate" if allowed else "refuse",
        "outcome": "pending" if allowed else "circuit_open",
    }


def _route_circuit(state: AnswerState) -> str:
    return state["route"]


async def _generate(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    context = _context(runtime)
    attempts = state.get("attempts", 0) + 1
    prompt = build_grounded_prompt(
        state["request"].query,
        state["chunks"],
        retry_instruction=attempts > 1,
    )
    started = time.monotonic()
    try:
        generation = await context.provider.generate(prompt)
    except ProviderTransientError:
        await context.circuit_breaker.record_transient_failure(_PROVIDER_NAME)
        return _failed_generation_update(state, context, attempts, started, usage=None)
    except ProviderResponseError as error:
        return _failed_generation_update(
            state,
            context,
            attempts,
            started,
            usage=error.usage,
        )
    except Exception:
        return {
            "attempts": attempts,
            "model_latency_ms": state.get("model_latency_ms", 0) + _latency_ms(started),
            "usage_complete": False,
            "route": "refuse",
            "outcome": "provider_error",
        }
    if not isinstance(generation, GeneratedAnswer):
        return _failed_generation_update(state, context, attempts, started, usage=None)
    return {
        "attempts": attempts,
        "generation": generation,
        "model_latency_ms": state.get("model_latency_ms", 0) + _latency_ms(started),
        "input_tokens": state.get("input_tokens", 0) + generation.input_tokens,
        "output_tokens": state.get("output_tokens", 0) + generation.output_tokens,
        "usage_complete": state.get("usage_complete", True) and generation.usage_complete,
        "route": "validate_citations",
        "outcome": "pending",
    }


def _failed_generation_update(
    state: AnswerState,
    context: AnswerRuntimeContext,
    attempts: int,
    started: float,
    *,
    usage: object | None,
) -> AnswerState:
    input_tokens = int(getattr(usage, "input_tokens", 0))
    output_tokens = int(getattr(usage, "output_tokens", 0))
    usage_complete = bool(getattr(usage, "complete", False))
    retry = attempts < context.max_attempts
    return {
        "attempts": attempts,
        "model_latency_ms": state.get("model_latency_ms", 0) + _latency_ms(started),
        "input_tokens": state.get("input_tokens", 0) + input_tokens,
        "output_tokens": state.get("output_tokens", 0) + output_tokens,
        "usage_complete": state.get("usage_complete", True) and usage_complete,
        "route": "check_circuit" if retry else "refuse",
        "outcome": "pending" if retry else "provider_error",
    }


def _route_generation(state: AnswerState) -> str:
    return state["route"]


async def _validate_citations(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    context = _context(runtime)
    request = state["request"]
    try:
        citations = context.validator.validate(
            state["generation"],
            state["chunks"],
            request.principal,
            request.knowledge_base_id,
        )
    except (GroundednessError, ValueError):
        retry = state["attempts"] < context.max_attempts
        return {
            "route": "check_circuit" if retry else "refuse",
            "outcome": "pending" if retry else "validation_refusal",
        }
    return {
        "citation_chunks": citations,
        "route": "validated",
        "outcome": "validated",
    }


def _route_validation(state: AnswerState) -> str:
    return state["route"]


async def _validated(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    context = _context(runtime)
    request = state["request"]
    generation = state["generation"]
    await context.circuit_breaker.record_success(_PROVIDER_NAME)
    input_tokens = state.get("input_tokens", 0)
    output_tokens = state.get("output_tokens", 0)
    citations = state["citation_chunks"]
    answer = ValidatedAnswer(
        text=generation.text,
        claims=generation.claims,
        citations=project_citations(citations, request.audience),
        segments=_segments(generation.text),
        refused=False,
        model=generation.model,
        prompt_version=PROMPT_VERSION,
        latency_ms=_latency_ms(state["started"]),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=_estimated_cost(
            input_tokens,
            output_tokens,
            context.input_cost_per_million,
            context.output_cost_per_million,
        ),
        usage_complete=state.get("usage_complete", False),
    )
    return {
        "execution": AnswerWorkflowExecution(
            answer=answer,
            retrieved_chunks=state["chunks"],
            retrieval_latency_ms=state.get("retrieval_latency_ms", 0),
            model_latency_ms=state.get("model_latency_ms", 0),
            source_citations=[citation_from_chunk(chunk) for chunk in citations],
            outcome="validated",
        )
    }


async def _refuse(
    state: AnswerState,
    runtime: Runtime[AnswerRuntimeContext],
) -> AnswerState:
    return {
        "execution": _refusal_execution(
            state["request"],
            _context(runtime),
            started=state["started"],
            chunks=state.get("chunks", []),
            retrieval_latency_ms=state.get("retrieval_latency_ms", 0),
            model_latency_ms=state.get("model_latency_ms", 0),
            input_tokens=state.get("input_tokens", 0),
            output_tokens=state.get("output_tokens", 0),
            outcome=state.get("outcome", "refused"),
        )
    }


class GroundedAnswerWorkflow:
    """Bounded, checkpointer-free LangGraph orchestration for one grounded answer."""

    def __init__(
        self,
        retriever: Retriever,
        provider: GenerationProvider,
        validator: CitationValidator,
        circuit_breaker: ProviderCircuitBreaker,
        *,
        refusal_message: str = _DEFAULT_REFUSAL,
        retrieval_limit: int = 8,
        max_attempts: int = 2,
        total_timeout_seconds: float = 60.0,
        input_cost_per_million: float = 0.0,
        output_cost_per_million: float = 0.0,
    ) -> None:
        self._context = AnswerRuntimeContext(
            retriever=retriever,
            provider=provider,
            validator=validator,
            circuit_breaker=circuit_breaker,
            refusal_message=refusal_message,
            retrieval_limit=retrieval_limit,
            max_attempts=max_attempts,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
        )
        self._total_timeout_seconds = total_timeout_seconds
        self._graph = _build_graph()

    async def run(self, request: AnswerWorkflowRequest) -> AnswerWorkflowExecution:
        started = time.monotonic()
        initial: AnswerState = {
            "request": request,
            "started": started,
            "chunks": [],
            "retrieval_latency_ms": 0,
            "model_latency_ms": 0,
            "attempts": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "usage_complete": True,
            "outcome": "pending",
        }
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                result = await self._graph.ainvoke(initial, context=self._context)
            execution = result.get("execution")
            if not isinstance(execution, AnswerWorkflowExecution):
                raise RuntimeError("answer workflow omitted its terminal execution")
            return execution
        except TimeoutError:
            return _refusal_execution(
                request,
                self._context,
                started=started,
                chunks=[],
                outcome="timeout",
            )
        except Exception:
            return _refusal_execution(
                request,
                self._context,
                started=started,
                chunks=[],
                outcome="workflow_error",
            )


def _build_graph() -> Any:
    builder = StateGraph(AnswerState, context_schema=AnswerRuntimeContext)
    builder.add_node("validate_scope", _validate_scope)
    builder.add_node("retrieve", _retrieve)
    builder.add_node("check_circuit", _check_circuit)
    builder.add_node("generate", _generate)
    builder.add_node("validate_citations", _validate_citations)
    builder.add_node("validated", _validated)
    builder.add_node("refuse", _refuse)
    builder.add_edge(START, "validate_scope")
    builder.add_conditional_edges(
        "validate_scope", _route_scope, {"retrieve": "retrieve", "refuse": "refuse"}
    )
    builder.add_conditional_edges(
        "retrieve",
        _route_evidence,
        {"check_circuit": "check_circuit", "refuse": "refuse"},
    )
    builder.add_conditional_edges(
        "check_circuit", _route_circuit, {"generate": "generate", "refuse": "refuse"}
    )
    builder.add_conditional_edges(
        "generate",
        _route_generation,
        {
            "validate_citations": "validate_citations",
            "check_circuit": "check_circuit",
            "refuse": "refuse",
        },
    )
    builder.add_conditional_edges(
        "validate_citations",
        _route_validation,
        {"validated": "validated", "check_circuit": "check_circuit", "refuse": "refuse"},
    )
    builder.add_edge("validated", END)
    builder.add_edge("refuse", END)
    return builder.compile()


def _refusal_execution(
    request: AnswerWorkflowRequest,
    context: AnswerRuntimeContext,
    *,
    started: float,
    chunks: list[RetrievedChunk],
    retrieval_latency_ms: int = 0,
    model_latency_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    outcome: str,
) -> AnswerWorkflowExecution:
    answer = ValidatedAnswer(
        text=context.refusal_message,
        claims=[],
        citations=[],
        segments=_segments(context.refusal_message),
        refused=True,
        model=_PROVIDER_NAME,
        prompt_version=PROMPT_VERSION,
        latency_ms=_latency_ms(started),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=_estimated_cost(
            input_tokens,
            output_tokens,
            context.input_cost_per_million,
            context.output_cost_per_million,
        ),
        usage_complete=False,
    )
    return AnswerWorkflowExecution(
        answer=answer,
        retrieved_chunks=chunks,
        retrieval_latency_ms=retrieval_latency_ms,
        model_latency_ms=model_latency_ms,
        outcome=outcome,
    )


def _latency_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


def _estimated_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate: float,
    output_rate: float,
) -> float:
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


def _segments(text: str) -> list[str]:
    return [segment for segment in _SENTENCES.split(text.strip()) if segment]
