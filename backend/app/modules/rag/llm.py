import json
import time
from dataclasses import dataclass
from typing import Protocol, cast

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.rag.prompts import GroundedPrompt
from app.modules.rag.types import ClaimSupport


class ProviderTransientError(RuntimeError):
    """A provider error that is safe to count towards the short-lived circuit."""


class ProviderResponseError(RuntimeError):
    """The provider returned output outside the strict generation contract."""


class GeneratedAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(max_length=16_000)
    claims: list[ClaimSupport] = Field(default_factory=list)
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _StructuredGeneration(BaseModel):
    """The exact JSON schema accepted from Claude before provider metadata is attached."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=16_000)
    claims: list[ClaimSupport] = Field(default_factory=list)


class GenerationProvider(Protocol):
    async def generate(self, prompt: GroundedPrompt) -> GeneratedAnswer: ...


class _AnthropicMessages(Protocol):
    async def create(self, **kwargs: object) -> object: ...


class _AnthropicClient(Protocol):
    messages: _AnthropicMessages


class AnthropicGenerationProvider:
    """The only generation provider; it does not expose tools or a fallback model."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-3-5-sonnet-latest",
        base_url: str | None = None,
        client: _AnthropicClient | None = None,
    ) -> None:
        self._model = model
        self._client: _AnthropicClient = (
            client
            if client is not None
            else cast(
                _AnthropicClient,
                (
                    AsyncAnthropic(api_key=api_key, base_url=base_url)
                    if base_url
                    else AsyncAnthropic(api_key=api_key)
                ),
            )
        )

    async def generate(self, prompt: GroundedPrompt) -> GeneratedAnswer:
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1_024,
                system=prompt.system_message,
                messages=[{"role": "user", "content": prompt.user_message}],
            )
        except Exception as error:
            if _is_transient_provider_error(error):
                raise ProviderTransientError("Claude temporarily unavailable") from error
            raise
        try:
            payload = _response_text(response)
            parsed = _StructuredGeneration.model_validate_json(payload)
            usage = getattr(response, "usage")
            return GeneratedAnswer(
                text=parsed.text,
                claims=parsed.claims,
                model=str(getattr(response, "model", self._model)),
                input_tokens=int(getattr(usage, "input_tokens", 0)),
                output_tokens=int(getattr(usage, "output_tokens", 0)),
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as error:
            raise ProviderResponseError("Claude returned invalid structured answer data") from error
        finally:
            _ = time.monotonic() - started


def _response_text(response: object) -> str:
    content = getattr(response, "content")
    if not isinstance(content, list) or len(content) != 1:
        raise ValueError("expected one Claude text block")
    text = getattr(content[0], "text", None)
    if not isinstance(text, str):
        raise ValueError("expected Claude text block")
    return text


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
