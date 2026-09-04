import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis

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

    async def eval(self, _script: str, numkeys: int, *args: object) -> list[int]:
        keys = tuple(str(item) for item in args[:numkeys])
        timestamp, threshold, limit, window_seconds, member = args[numkeys:]
        for key in keys:
            await self.zremrangebyscore(key, 0, float(threshold))
        blocked = [key for key in keys if await self.zcard(key) >= int(limit)]
        if blocked:
            oldest = await self.zrange(blocked[0], 0, 0, withscores=True)
            return [0, int(float(oldest[0][1]) + int(window_seconds) - float(timestamp)) + 1]
        for key in keys:
            await self.zadd(key, {str(member): float(timestamp)})
            await self.expire(key, int(window_seconds) + 1)
        return [1, 0]


class AtomicFakeRedis(FakeRedis):
    """Small Redis EVAL contract fake: all keys are checked before any is changed."""

    def __init__(self) -> None:
        super().__init__()
        self.eval_calls = 0

    async def eval(self, _script: str, numkeys: int, *args: object) -> list[int]:
        self.eval_calls += 1
        keys = tuple(str(item) for item in args[:numkeys])
        timestamp, threshold, limit, window_seconds, member = args[numkeys:]
        for key in keys:
            await self.zremrangebyscore(key, 0, float(threshold))
        blocked = [key for key in keys if await self.zcard(key) >= int(limit)]
        if blocked:
            oldest = await self.zrange(blocked[0], 0, 0, withscores=True)
            return [0, int(float(oldest[0][1]) + int(window_seconds) - float(timestamp)) + 1]
        for key in keys:
            await self.zadd(key, {str(member): float(timestamp)})
            await self.expire(key, int(window_seconds) + 1)
        return [1, 0]


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


@pytest.mark.asyncio
async def test_atomic_script_never_partially_admits_a_multi_window_request() -> None:
    redis = AtomicFakeRedis()
    limiter = SlidingWindowRateLimiter(redis, creation_limit=1, message_limit=2)
    now = datetime.now(UTC)

    await limiter.check_creation(ip_address="203.0.113.7", organization_id="org-a", now=now)
    with pytest.raises(RateLimitExceeded):
        await limiter.check_creation(ip_address="203.0.113.8", organization_id="org-a", now=now)

    assert redis.eval_calls == 2
    assert redis.entries["chat:rate:create:ip:203.0.113.8"] == []


@pytest.mark.asyncio
async def test_live_redis_script_enforces_limit_and_returns_retry_after() -> None:
    redis = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    marker = uuid4().hex
    ip_address = f"task13-{marker}"
    organization_id = f"org-{marker}"
    limiter = SlidingWindowRateLimiter(redis, creation_limit=1, message_limit=1)
    keys = (
        f"chat:rate:create:ip:{ip_address}",
        f"chat:rate:create:organization:{organization_id}",
    )
    try:
        await limiter.check_creation(ip_address=ip_address, organization_id=organization_id)
        with pytest.raises(RateLimitExceeded) as error:
            await limiter.check_creation(ip_address=ip_address, organization_id=organization_id)
        assert 1 <= error.value.retry_after <= 60
    finally:
        await redis.delete(*keys)
        await redis.aclose()
