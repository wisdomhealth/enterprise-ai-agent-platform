from datetime import UTC, datetime, timedelta
from secrets import token_hex
from typing import Any, Protocol


class SlidingWindowRedis(Protocol):
    async def eval(self, script: str, numkeys: int, *args: object) -> list[Any]: ...


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Please wait a moment before trying again.")
        self.retry_after = retry_after


class RateLimitUnavailable(RuntimeError):
    """Public writes fail closed if their admission control is unavailable."""


_SLIDING_WINDOW_ADMIT = """
local now = tonumber(ARGV[1])
local threshold = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local window = tonumber(ARGV[4])
local member = ARGV[5]
local retry_after = 0

for index = 1, #KEYS do
  redis.call('ZREMRANGEBYSCORE', KEYS[index], 0, threshold)
  if redis.call('ZCARD', KEYS[index]) >= limit then
    local oldest = redis.call('ZRANGE', KEYS[index], 0, 0, 'WITHSCORES')
    local retry = window
    if oldest[2] then
      retry = math.max(1, math.ceil(tonumber(oldest[2]) + window - now))
    end
    if retry > retry_after then retry_after = retry end
  end
end

if retry_after > 0 then return {0, retry_after} end
for index = 1, #KEYS do
  redis.call('ZADD', KEYS[index], now, member)
  redis.call('EXPIRE', KEYS[index], window + 1)
end
return {1, 0}
"""


class SlidingWindowRateLimiter:
    """Atomic Redis sliding-window admission for public chat ingress."""

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

    async def check_creation_ip(
        self, *, ip_address: str, now: datetime | None = None
    ) -> None:
        await self._check_many(
            (f"chat:rate:create:ip:{ip_address}",), self._creation_limit, now=now
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
        try:
            result = await self._redis.eval(
                _SLIDING_WINDOW_ADMIT,
                len(keys),
                *keys,
                timestamp,
                threshold,
                limit,
                self._window_seconds,
                token_hex(16),
            )
        except Exception as error:
            raise RateLimitUnavailable from error
        admitted = int(result[0]) if result else 0
        if admitted != 1:
            retry_after = int(result[1]) if len(result) > 1 else self._window_seconds
            raise RateLimitExceeded(max(1, retry_after))
