import pytest

from app.modules.rag.llm import InMemoryRedisCircuitStore, ProviderCircuitBreaker


@pytest.mark.asyncio
async def test_provider_circuit_opens_without_selecting_fallback() -> None:
    circuit_breaker = ProviderCircuitBreaker(InMemoryRedisCircuitStore(), failure_threshold=5)

    for _ in range(5):
        await circuit_breaker.record_transient_failure("claude")

    assert await circuit_breaker.allow("claude") is False
    assert circuit_breaker.fallback_provider is None


@pytest.mark.asyncio
async def test_provider_success_clears_only_ephemeral_failure_state() -> None:
    store = InMemoryRedisCircuitStore()
    circuit_breaker = ProviderCircuitBreaker(store, failure_threshold=2)
    await circuit_breaker.record_transient_failure("claude")
    await circuit_breaker.record_success("claude")

    assert await circuit_breaker.allow("claude") is True
    assert store.max_ttl_seconds > 0
