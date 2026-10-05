import pytest

from app.core.openai import (
    ModelUsage,
    OpenAIStructuredResponseError,
    StructuredModelResult,
)
from app.modules.rag.llm import (
    OpenAIGenerationProvider,
    ProviderResponseError,
    ProviderTransientError,
    _StructuredGeneration,
)
from app.modules.rag.prompts import build_grounded_prompt


class _StructuredClient:
    def __init__(self, result: object) -> None:
        self.result = result

    async def invoke(self, *_args: object, **_kwargs: object) -> object:
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_openai_provider_attaches_only_trusted_response_metadata() -> None:
    provider = OpenAIGenerationProvider(
        _StructuredClient(
            StructuredModelResult(
                value=_StructuredGeneration(
                    text="Refunds take five business days.", claims=[]
                ),
                model="gpt-test",
                usage=ModelUsage(input_tokens=11, output_tokens=7, complete=True),
            )
        )
    )

    answer = await provider.generate(build_grounded_prompt("When?", []))

    assert answer.text == "Refunds take five business days."
    assert answer.model == "gpt-test"
    assert answer.input_tokens == 11
    assert answer.output_tokens == 7
    assert answer.usage_complete is True


@pytest.mark.asyncio
async def test_openai_provider_preserves_missing_usage_state() -> None:
    provider = OpenAIGenerationProvider(
        _StructuredClient(
            StructuredModelResult(
                value=_StructuredGeneration(text="Answer.", claims=[]),
                model="gpt-test",
                usage=ModelUsage(input_tokens=0, output_tokens=0, complete=False),
            )
        )
    )

    answer = await provider.generate(build_grounded_prompt("Question?", []))

    assert answer.input_tokens == 0
    assert answer.output_tokens == 0
    assert answer.usage_complete is False


@pytest.mark.asyncio
async def test_openai_provider_maps_timeout_to_transient_error() -> None:
    provider = OpenAIGenerationProvider(_StructuredClient(TimeoutError("provider detail")))

    with pytest.raises(ProviderTransientError, match="OpenAI temporarily unavailable"):
        await provider.generate(build_grounded_prompt("Question?", []))


@pytest.mark.asyncio
async def test_openai_provider_maps_invalid_structure_and_retains_known_usage() -> None:
    source_error = OpenAIStructuredResponseError(
        usage=ModelUsage(input_tokens=5, output_tokens=3, complete=True)
    )
    provider = OpenAIGenerationProvider(_StructuredClient(source_error))

    with pytest.raises(ProviderResponseError) as caught:
        await provider.generate(build_grounded_prompt("Question?", []))

    assert caught.value.usage == ModelUsage(input_tokens=5, output_tokens=3, complete=True)
    assert "provider detail" not in str(caught.value)
