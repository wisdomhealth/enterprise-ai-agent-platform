from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from app.core.openai import (
    LangChainStructuredClient,
    ModelUsage,
    OpenAIStructuredResponseError,
    StructuredModelResult,
    build_structured_openai_client,
)
from app.modules.rag.prompts import GroundedPrompt
from app.modules.rag.types import ClaimSupport

if TYPE_CHECKING:
    from app.core.config import Settings


class ProviderTransientError(RuntimeError):
    """A provider error that is safe to count towards the short-lived circuit."""


class ProviderResponseError(RuntimeError):
    """The provider returned output outside the strict generation contract."""

    def __init__(self, message: str, *, usage: ModelUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or ModelUsage(input_tokens=0, output_tokens=0, complete=False)


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(max_length=16_000)
    claims: list[ClaimSupport] = Field(default_factory=list)
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    usage_complete: bool = True


class _StructuredGeneration(BaseModel):
    """The exact JSON schema accepted before trusted provider metadata is attached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=16_000)
    claims: list[ClaimSupport] = Field(default_factory=list)


class GenerationProvider(Protocol):
    async def generate(self, prompt: GroundedPrompt) -> GeneratedAnswer: ...


class _GenerationStructuredClient(Protocol):
    async def invoke(
        self,
        schema: type[_StructuredGeneration],
        *,
        system: str,
        user: str,
    ) -> StructuredModelResult[_StructuredGeneration]: ...


class OpenAIGenerationProvider:
    """Grounded generation through the shared validated LangChain OpenAI boundary."""

    def __init__(self, client: object) -> None:
        self._client = cast(_GenerationStructuredClient, client)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAIGenerationProvider:
        client: LangChainStructuredClient = build_structured_openai_client(
            settings,
            model_name=settings.openai_generation_model,
        )
        return cls(client)

    async def generate(self, prompt: GroundedPrompt) -> GeneratedAnswer:
        try:
            result = await self._client.invoke(
                _StructuredGeneration,
                system=prompt.system_message,
                user=prompt.user_message,
            )
        except OpenAIStructuredResponseError as error:
            raise ProviderResponseError(
                "OpenAI returned invalid structured answer data",
                usage=error.usage,
            ) from error
        except Exception as error:
            if _is_transient_provider_error(error):
                raise ProviderTransientError("OpenAI temporarily unavailable") from error
            raise
        return GeneratedAnswer(
            text=result.value.text,
            claims=result.value.claims,
            model=result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            usage_complete=result.usage.complete,
        )


def _is_transient_provider_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    return isinstance(error, (TimeoutError, ConnectionError)) or status_code in {
        408,
        429,
        500,
        502,
        503,
        504,
    }


class CircuitStore(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def incr(self, key: str) -> int: ...

    async def expire(self, key: str, seconds: int) -> bool: ...

    async def set(self, key: str, value: str, *, ex: int) -> bool | None: ...

    async def delete(self, *keys: str) -> int: ...


class RedisCircuitStore:
    """Thin adapter over redis.asyncio; circuit data only uses expiring keys."""

    def __init__(self, client: CircuitStore) -> None:
        self._client = client

    async def get(self, key: str) -> bytes | str | None:
        return await self._client.get(key)

    async def incr(self, key: str) -> int:
        return await self._client.incr(key)

    async def expire(self, key: str, seconds: int) -> bool:
        return await self._client.expire(key, seconds)

    async def set(self, key: str, value: str, *, ex: int) -> bool | None:
        return await self._client.set(key, value, ex=ex)

    async def delete(self, *keys: str) -> int:
        return await self._client.delete(*keys)


class ProviderCircuitBreaker:
    """A bounded Redis cache guard, deliberately not durable workflow state."""

    fallback_provider: None = None

    def __init__(
        self,
        store: CircuitStore,
        *,
        failure_threshold: int = 5,
        reset_seconds: int = 30,
        key_prefix: str = "rag:provider-circuit",
    ) -> None:
        self._store = store
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._key_prefix = key_prefix

    def _failure_key(self, provider: str) -> str:
        return f"{self._key_prefix}:failures:{provider}"

    def _open_key(self, provider: str) -> str:
        return f"{self._key_prefix}:open:{provider}"

    async def allow(self, provider: str) -> bool:
        return await self._store.get(self._open_key(provider)) is None

    async def record_transient_failure(self, provider: str) -> None:
        failures = await self._store.incr(self._failure_key(provider))
        if failures == 1:
            await self._store.expire(self._failure_key(provider), self._reset_seconds)
        if failures >= self._failure_threshold:
            await self._store.set(self._open_key(provider), "1", ex=self._reset_seconds)

    async def record_success(self, provider: str) -> None:
        await self._store.delete(self._failure_key(provider), self._open_key(provider))


@dataclass(slots=True)
class _Entry:
    value: str
    expires_at: float


class InMemoryRedisCircuitStore:
    """Test-only ephemeral store with Redis-compatible TTL semantics."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}
        self.max_ttl_seconds = 0

    def _entry(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is not None and entry.expires_at <= time.monotonic():
            del self._entries[key]
            return None
        return entry

    async def get(self, key: str) -> str | None:
        entry = self._entry(key)
        return entry.value if entry is not None else None

    async def incr(self, key: str) -> int:
        entry = self._entry(key)
        value = int(entry.value) + 1 if entry is not None else 1
        expires_at = entry.expires_at if entry is not None else time.monotonic() + 86_400
        self._entries[key] = _Entry(str(value), expires_at)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        entry = self._entry(key)
        if entry is None:
            return False
        self.max_ttl_seconds = max(self.max_ttl_seconds, seconds)
        self._entries[key] = _Entry(entry.value, time.monotonic() + seconds)
        return True

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.max_ttl_seconds = max(self.max_ttl_seconds, ex)
        self._entries[key] = _Entry(value, time.monotonic() + ex)
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if self._entry(key) is not None:
                del self._entries[key]
                deleted += 1
        return deleted
