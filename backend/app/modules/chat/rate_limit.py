from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any, Protocol


class SlidingWindowRedis(Protocol):
    async def zremrangebyscore(self, key: str, minimum: float, maximum: float) -> Any: ...

    async def zcard(self, key: str) -> int: ...

    async def zadd(self, key: str, mapping: dict[str, float]) -> Any: ...

    async def expire(self, key: str, seconds: int) -> Any: ...

    async def zrange(
        self, key: str, start: int, end: int, *, withscores: bool = False
    ) -> list[Any]: ...


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Please wait a moment before trying again.")
        self.retry_after = retry_after


class SlidingWindowRateLimiter:
    """Redis-backed fixed-duration sliding windows for public chat ingress."""

    def __init__(
        self,
        redis: SlidingWindowRedis,
        *,
        creation_limit: int = 10,
        message_limit: int = 30,
        window_seconds: int = 60,
    ) -> None:
        self._redis = redis
        self._creation_limit = creation_limit
        self._message_limit = message_limit
        self._window_seconds = window_seconds

    async def check_creation(
        self, *, ip_address: str, organization_id: str, now: datetime | None = None
    ) -> None:
        await self._check_many(
            (
                f"chat:rate:create:ip:{ip_address}",
                f"chat:rate:create:organization:{organization_id}",
            ),
            self._creation_limit,
            now=now,
        )

    async def check_message(
        self,
        *,
        session_id: str,
        organization_id: str,
        ip_address: str = "unknown",
        now: datetime | None = None,
    ) -> None:
        await self._check_many(
            (
                f"chat:rate:message:ip:{ip_address}",
                f"chat:rate:message:session:{session_id}",
                f"chat:rate:message:organization:{organization_id}",
            ),
            self._message_limit,
            now=now,
        )

    async def _check_many(
        self, keys: tuple[str, ...], limit: int, *, now: datetime | None
    ) -> None:
        instant = now or datetime.now(UTC)
        timestamp = instant.timestamp()
        threshold = (instant - timedelta(seconds=self._window_seconds)).timestamp()
        retries: list[int] = []
        for key in keys:
            await self._redis.zremrangebyscore(key, 0, threshold)
            if await self._redis.zcard(key) >= limit:
                oldest = await self._redis.zrange(key, 0, 0, withscores=True)
                if oldest:
                    raw_score = oldest[0][1]
                    score = float(raw_score.decode() if isinstance(raw_score, bytes) else raw_score)
                    retries.append(max(1, int(score + self._window_seconds - timestamp) + 1))
                else:
                    retries.append(self._window_seconds)
        if retries:
            raise RateLimitExceeded(max(retries))
        for key in keys:
            await self._redis.zadd(key, {token_hex(16): timestamp})
            await self._redis.expire(key, self._window_seconds + 1)
