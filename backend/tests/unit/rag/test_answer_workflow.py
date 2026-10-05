import asyncio
from uuid import uuid4

import pytest

from app.modules.identity.dependencies import Principal
from app.modules.identity.models import UserRole
from app.modules.rag.groundedness import CitationValidator
from app.modules.rag.llm import (
    GeneratedAnswer,
    InMemoryRedisCircuitStore,
    ProviderCircuitBreaker,
    ProviderResponseError,
    ProviderTransientError,
)
from app.modules.rag.types import AnswerAudience, ClaimSupport, RetrievedChunk
from app.modules.rag.workflow import (
    AnswerWorkflowRequest,
    GroundedAnswerWorkflow,
)


class _Retriever:
    def __init__(self, result: list[RetrievedChunk] | BaseException) -> None:
        self.result = result

    async def retrieve(self, *_args: object, **_kwargs: object) -> list[RetrievedChunk]:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Provider:
    def __init__(self, answers: list[GeneratedAnswer | BaseException]) -> None:
        self.answers = answers
        self.calls = 0
        self.prompts = []

    async def generate(self, prompt):  # type: ignore[no-untyped-def]
        self.prompts.append(prompt)
        answer = self.answers[self.calls]
        self.calls += 1
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _SlowProvider:
    calls = 0

    async def generate(self, prompt):  # type: ignore[no-untyped-def]
        self.calls += 1
        await asyncio.sleep(1)
        raise AssertionError("timeout did not cancel generation")


def _scope() -> tuple[Principal, RetrievedChunk]:
    principal = Principal(
        uuid4(), uuid4(), "member@example.test", UserRole.MEMBER, uuid4(), "csrf"
    )
    chunk = RetrievedChunk(
        chunk_id=uuid4(),
        stable_id="refund-policy",
        document_version_id=uuid4(),
        document_id=uuid4(),
        organization_id=principal.organization_id,
        knowledge_base_id=uuid4(),
        ordinal=0,
        text="Refunds take five business days.",
        page_number=2,
        section="Refunds",
        resource_authorized=True,
        title="Refund policy",
    )
    return principal, chunk


def _generation(
    chunk: RetrievedChunk,
    *,
    claim_text: str = "Refunds take five business days.",
    input_tokens: int = 10,
    output_tokens: int = 6,
    usage_complete: bool = True,
) -> GeneratedAnswer:
    return GeneratedAnswer(
        text=claim_text,
        claims=[ClaimSupport(text=claim_text, citation_ids=[chunk.chunk_id])],
        model="gpt-test",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_complete=usage_complete,
    )


def _workflow(
    chunks: list[RetrievedChunk] | BaseException,
    provider: object,
    *,
    max_attempts: int = 2,
    timeout: float = 1,
    circuit: ProviderCircuitBreaker | None = None,
) -> GroundedAnswerWorkflow:
    return GroundedAnswerWorkflow(
        _Retriever(chunks),
        provider,  # type: ignore[arg-type]
        CitationValidator(),
        circuit or ProviderCircuitBreaker(InMemoryRedisCircuitStore()),
        max_attempts=max_attempts,
        total_timeout_seconds=timeout,
    )


def _request(principal: Principal, chunk: RetrievedChunk) -> AnswerWorkflowRequest:
    return AnswerWorkflowRequest(
        principal=principal,
        knowledge_base_id=chunk.knowledge_base_id,
        query="How long do refunds take?",
        audience=AnswerAudience.STAFF,
    )


@pytest.mark.asyncio
async def test_graph_generates_once_and_validates_supported_answer() -> None:
    principal, chunk = _scope()
    provider = _Provider([_generation(chunk)])

    execution = await _workflow([chunk], provider).run(_request(principal, chunk))

    assert execution.answer.refused is False
    assert provider.calls == 1
    assert execution.answer.usage_complete is True


@pytest.mark.asyncio
async def test_graph_refuses_without_calling_model_when_evidence_is_empty() -> None:
    principal, chunk = _scope()
    provider = _Provider([_generation(chunk)])

    execution = await _workflow([], provider).run(_request(principal, chunk))

    assert execution.answer.refused is True
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_graph_retries_correctable_validation_once_then_stops() -> None:
    principal, chunk = _scope()
    provider = _Provider(
        [
            _generation(chunk, claim_text="Refunds take ten days."),
            _generation(chunk),
        ]
    )

    execution = await _workflow([chunk], provider).run(_request(principal, chunk))

    assert execution.answer.refused is False
    assert provider.calls == 2
    assert execution.answer.input_tokens == 20
    assert execution.answer.output_tokens == 12
    assert provider.prompts[1].retry_instruction is True


@pytest.mark.asyncio
async def test_graph_does_not_retry_authorization_failure() -> None:
    principal, chunk = _scope()
    provider = _Provider([_generation(chunk)])

    execution = await _workflow(PermissionError(), provider).run(
        _request(principal, chunk)
    )

    assert execution.answer.refused is True
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_graph_aggregates_known_error_usage_and_marks_missing_usage() -> None:
    principal, chunk = _scope()
    error = ProviderResponseError("bad structure")
    error.usage = error.usage.__class__(4, 2, False)
    provider = _Provider([error, _generation(chunk, input_tokens=8, output_tokens=4)])

    execution = await _workflow([chunk], provider).run(_request(principal, chunk))

    assert execution.answer.input_tokens == 12
    assert execution.answer.output_tokens == 6
    assert execution.answer.usage_complete is False
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_graph_stops_after_transient_retry_exhaustion() -> None:
    principal, chunk = _scope()
    provider = _Provider([ProviderTransientError(), ProviderTransientError()])

    execution = await _workflow([chunk], provider).run(_request(principal, chunk))

    assert execution.answer.refused is True
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_graph_refuses_when_circuit_is_open() -> None:
    principal, chunk = _scope()
    provider = _Provider([_generation(chunk)])
    circuit = ProviderCircuitBreaker(InMemoryRedisCircuitStore(), failure_threshold=1)
    await circuit.record_transient_failure("openai")

    execution = await _workflow([chunk], provider, circuit=circuit).run(
        _request(principal, chunk)
    )

    assert execution.answer.refused is True
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_graph_total_timeout_cancels_model_and_refuses() -> None:
    principal, chunk = _scope()
    provider = _SlowProvider()

    execution = await _workflow([chunk], provider, timeout=0.1).run(
        _request(principal, chunk)
    )

    assert execution.answer.refused is True
    assert provider.calls == 1
