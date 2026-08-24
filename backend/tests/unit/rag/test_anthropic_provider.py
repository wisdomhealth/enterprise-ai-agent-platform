import pytest

from app.modules.rag.llm import AnthropicGenerationProvider, ProviderResponseError
from app.modules.rag.prompts import build_grounded_prompt


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    input_tokens = 11
    output_tokens = 7


class _Response:
    model = "claude-test"
    usage = _Usage()

    def __init__(self, text: str) -> None:
        self.content = [_TextBlock(text)]


class _Messages:
    def __init__(self, response: _Response) -> None:
        self.response = response

    async def create(self, **kwargs: object) -> _Response:
        return self.response


class _Client:
    def __init__(self, response: _Response) -> None:
        self.messages = _Messages(response)


@pytest.mark.asyncio
async def test_anthropic_provider_validates_json_before_returning_generation() -> None:
    provider = AnthropicGenerationProvider(
        "test-key",
        client=_Client(
            _Response(
                '{"text":"Refunds take five business days.",'
                '"claims":[{"text":"Refunds take five business days.","citation_ids":[]}]}'
            )
        ),
    )

    answer = await provider.generate(build_grounded_prompt("When?", []))

    assert answer.model == "claude-test"
    assert answer.input_tokens == 11
    assert answer.output_tokens == 7
    assert answer.claims[0].text == "Refunds take five business days."


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_non_json_output() -> None:
    provider = AnthropicGenerationProvider("test-key", client=_Client(_Response("not json")))

    with pytest.raises(ProviderResponseError):
        await provider.generate(build_grounded_prompt("When?", []))


@pytest.mark.asyncio
async def test_anthropic_provider_rejects_json_outside_the_output_schema() -> None:
    provider = AnthropicGenerationProvider(
        "test-key",
        client=_Client(_Response('{"text":"Answer.","claims":[],"unexpected":"field"}')),
    )

    with pytest.raises(ProviderResponseError):
        await provider.generate(build_grounded_prompt("When?", []))
