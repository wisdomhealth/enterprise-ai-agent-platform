from datetime import UTC, datetime, timedelta

import pytest

from app.modules.chat.rate_limit import RateLimitExceeded, SlidingWindowRateLimiter


class FakeRedis:
    def __init__(self) -> None:
        self.entries: dict[str, list[tuple[str, float]]] = {}

    async def zremrangebyscore(self, key: str, _minimum: float, maximum: float) -> int:
        existing = self.entries.get(key, [])
        self.entries[key] = [(member, score) for member, score in existing if score > maximum]
        return len(existing) - len(self.entries[key])

    async def zcard(self, key: str) -> int:
        return len(self.entries.get(key, []))

    async def zadd(self, key: str, values: dict[str, float]) -> int:
        self.entries.setdefault(key, []).extend(values.items())
        return len(values)

    async def expire(self, _key: str, _seconds: int) -> bool:
        return True

    async def zrange(self, key: str, start: int, end: int, *, withscores: bool = False):
        values = sorted(self.entries.get(key, []), key=lambda item: item[1])[start : end + 1]
        return values if withscores else [value[0] for value in values]


@pytest.mark.asyncio
async def test_creation_limit_is_applied_independently_per_ip_and_organization() -> None:
    limiter = SlidingWindowRateLimiter(FakeRedis(), creation_limit=1, message_limit=2)
    now = datetime.now(UTC)

    await limiter.check_creation(ip_address="203.0.113.7", organization_id="org-a", now=now)
    with pytest.raises(RateLimitExceeded) as error:
        await limiter.check_creation(ip_address="203.0.113.7", organization_id="org-a", now=now)

    assert error.value.retry_after >= 1
    with pytest.raises(RateLimitExceeded):
        await limiter.check_creation(ip_address="203.0.113.8", organization_id="org-a", now=now)
    await limiter.check_creation(ip_address="203.0.113.8", organization_id="org-b", now=now)


@pytest.mark.asyncio
async def test_message_limit_is_applied_per_session_and_organization() -> None:
    limiter = SlidingWindowRateLimiter(FakeRedis(), creation_limit=2, message_limit=1)
    now = datetime.now(UTC)

    await limiter.check_message(session_id="session-a", organization_id="org-a", now=now)
    with pytest.raises(RateLimitExceeded):
        await limiter.check_message(session_id="session-a", organization_id="org-a", now=now)
    with pytest.raises(RateLimitExceeded):
        await limiter.check_message(
            session_id="session-b", organization_id="org-a", now=now + timedelta(seconds=1)
        )
    await limiter.check_message(
        session_id="session-b", organization_id="org-b", ip_address="203.0.113.8", now=now
    )
