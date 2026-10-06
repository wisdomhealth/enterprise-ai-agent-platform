from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict

from app.core.openai import LangChainStructuredClient, OpenAIStructuredResponseError


class _AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class _Runnable:
    def __init__(self, response: object) -> None:
        self.response = response
        self.messages: object | None = None

    async def ainvoke(self, messages: object) -> object:
        self.messages = messages
        return self.response


class _Model:
    def __init__(self, response: object) -> None:
        self.runnable = _Runnable(response)
        self.options: dict[str, Any] | None = None

    def with_structured_output(self, schema: object, **kwargs: Any) -> _Runnable:
        self.options = {"schema": schema, **kwargs}
        return self.runnable


def _client_returning(raw: AIMessage, parsed: object) -> tuple[LangChainStructuredClient, _Model]:
    model = _Model({"raw": raw, "parsed": parsed, "parsing_error": None})
    return LangChainStructuredClient(model), model


@pytest.mark.asyncio
async def test_structured_client_reads_model_and_usage_from_raw_message() -> None:
    raw = AIMessage(
        content="",
        response_metadata={"model_name": "gpt-test"},
        usage_metadata={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    )
    client, model = _client_returning(raw, _AnswerPayload(text="Answer"))

    result = await client.invoke(_AnswerPayload, system="rules", user="question")

    assert result.value == _AnswerPayload(text="Answer")
    assert result.model == "gpt-test"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.complete is True
    assert model.options == {
        "schema": _AnswerPayload,
        "method": "json_schema",
        "include_raw": True,
        "strict": True,
    }
    assert model.runnable.messages == [SystemMessage("rules"), HumanMessage("question")]


@pytest.mark.asyncio
async def test_structured_client_distinguishes_missing_usage_from_zero() -> None:
    client, _ = _client_returning(
        AIMessage(content="", response_metadata={"model_name": "gpt-test"}),
        _AnswerPayload(text="Answer"),
    )

    result = await client.invoke(_AnswerPayload, system="rules", user="question")

    assert result.usage.input_tokens == 0
    assert result.usage.output_tokens == 0
    assert result.usage.complete is False

    zero_client, _ = _client_returning(
        AIMessage(
            content="",
            response_metadata={"model_name": "gpt-test"},
            usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        ),
        _AnswerPayload(text="Answer"),
    )
    zero_result = await zero_client.invoke(
        _AnswerPayload, system="rules", user="question"
    )
    assert zero_result.usage.complete is True


@pytest.mark.asyncio
async def test_structured_client_rejects_missing_model_metadata() -> None:
    client, _ = _client_returning(
        AIMessage(
            content="",
            usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        ),
        _AnswerPayload(text="provider secret output"),
    )

    with pytest.raises(OpenAIStructuredResponseError) as caught:
        await client.invoke(_AnswerPayload, system="rules", user="question")

    assert "provider secret output" not in str(caught.value)
    assert caught.value.usage.complete is True


@pytest.mark.asyncio
async def test_structured_client_rejects_parsing_errors_without_exposing_output() -> None:
    raw = AIMessage(
        content="provider secret output",
        response_metadata={"model_name": "gpt-test"},
        usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
    )
    model = _Model(
        {"raw": raw, "parsed": None, "parsing_error": ValueError("provider secret output")}
    )

    with pytest.raises(OpenAIStructuredResponseError) as caught:
        await LangChainStructuredClient(model).invoke(
            _AnswerPayload, system="rules", user="question"
        )

    assert "provider secret output" not in str(caught.value)
    assert caught.value.usage.input_tokens == 5
    assert caught.value.usage.output_tokens == 3
