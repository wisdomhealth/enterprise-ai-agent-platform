from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

if TYPE_CHECKING:
    from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    complete: bool


@dataclass(frozen=True, slots=True)
class StructuredModelResult[SchemaT: BaseModel]:
    value: SchemaT
    model: str
    usage: ModelUsage


class OpenAIStructuredResponseError(RuntimeError):
    """The provider response did not satisfy the trusted structured-output contract."""

    def __init__(self, *, usage: ModelUsage | None = None) -> None:
        super().__init__("OpenAI returned invalid structured response data")
        self.usage = usage or ModelUsage(input_tokens=0, output_tokens=0, complete=False)


class _AsyncRunnable(Protocol):
    async def ainvoke(self, input: object) -> object: ...


class _StructuredModel(Protocol):
    def with_structured_output(
        self,
        schema: type[BaseModel],
        *,
        method: str,
        include_raw: bool,
        strict: bool,
    ) -> _AsyncRunnable: ...


class LangChainStructuredClient:
    """Single validated boundary for all LangChain structured OpenAI calls."""

    def __init__(self, model: object) -> None:
        self._model = cast(_StructuredModel, model)

    async def invoke[SchemaT: BaseModel](
        self,
        schema: type[SchemaT],
        *,
        system: str,
        user: str,
    ) -> StructuredModelResult[SchemaT]:
        runnable = self._model.with_structured_output(
            schema,
            method="json_schema",
            include_raw=True,
            strict=True,
        )
        response = await runnable.ainvoke([SystemMessage(system), HumanMessage(user)])
        return _validated_result(response, schema)


def build_structured_openai_client(
    settings: Settings,
    *,
    model_name: str,
) -> LangChainStructuredClient:
    if settings.openai_api_key is None:
        raise ValueError("OPENAI_API_KEY is required")

    kwargs: dict[str, Any] = {
        "api_key": settings.openai_api_key.get_secret_value(),
        "model": model_name,
        "timeout": settings.openai_request_timeout_seconds,
        "max_retries": 0,
        "temperature": 0,
    }
    if settings.openai_base_url is not None:
        kwargs["base_url"] = str(settings.openai_base_url)
    return LangChainStructuredClient(ChatOpenAI(**kwargs))


def _validated_result[SchemaT: BaseModel](
    response: object,
    schema: type[SchemaT],
) -> StructuredModelResult[SchemaT]:
    if not isinstance(response, dict):
        raise OpenAIStructuredResponseError()

    raw = response.get("raw")
    usage = _usage_from_raw(raw)
    if response.get("parsing_error") is not None:
        raise OpenAIStructuredResponseError(usage=usage)

    parsed = response.get("parsed")
    if not isinstance(parsed, schema):
        raise OpenAIStructuredResponseError(usage=usage)

    metadata = getattr(raw, "response_metadata", None)
    if not isinstance(metadata, dict):
        raise OpenAIStructuredResponseError(usage=usage)
    model = metadata.get("model_name") or metadata.get("model")
    if not isinstance(model, str) or not model.strip():
        raise OpenAIStructuredResponseError(usage=usage)

    return StructuredModelResult(value=parsed, model=model, usage=usage)


def _usage_from_raw(raw: object) -> ModelUsage:
    metadata = getattr(raw, "usage_metadata", None)
    if not isinstance(metadata, dict):
        return ModelUsage(input_tokens=0, output_tokens=0, complete=False)

    input_tokens = metadata.get("input_tokens")
    output_tokens = metadata.get("output_tokens")
    if (
        not isinstance(input_tokens, int)
        or isinstance(input_tokens, bool)
        or input_tokens < 0
        or not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 0
    ):
        return ModelUsage(input_tokens=0, output_tokens=0, complete=False)
    return ModelUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        complete=True,
    )
