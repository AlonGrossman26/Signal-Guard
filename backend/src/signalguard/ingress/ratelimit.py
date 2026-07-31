"""Per-endpoint rate limiting: a Redis token bucket (CLAUDE.md §8).

A bucket refills at a steady rate up to a burst capacity. That shape suits
trading signals better than a fixed window: a strategy legitimately fires several
alerts at once when a bar closes, but should not sustain that rate forever. A
fixed window would either reject the legitimate burst or permit double the
intended rate across a window boundary.

The whole check runs as one Lua script so it is atomic — read, refill, decide,
and write cannot interleave with another request.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 20        # burst
DEFAULT_REFILL_PER_SEC = 1.0  # sustained

# KEYS[1] = bucket key
# ARGV = capacity, refill_rate, now_ms, ttl_sec
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'updated_ms')
local tokens = tonumber(bucket[1])
local updated_ms = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  updated_ms = now_ms
end

-- Refill for elapsed time, capped at capacity.
local elapsed_sec = math.max(0, (now_ms - updated_ms) / 1000.0)
tokens = math.min(capacity, tokens + elapsed_sec * refill_rate)

local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end

redis.call('HMSET', key, 'tokens', tokens, 'updated_ms', now_ms)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens)}
"""


class RateLimiter:
    """Token bucket keyed by webhook endpoint."""

    def __init__(
        self,
        redis: Redis,
        capacity: int = DEFAULT_CAPACITY,
        refill_per_sec: float = DEFAULT_REFILL_PER_SEC,
    ) -> None:
        self._redis = redis
        self._capacity = capacity
        self._refill = refill_per_sec
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    async def allow(self, endpoint_hash: str, now_ms: int) -> bool:
        """Consume one token. False means the caller is over its limit.

        On a Redis failure this returns **False** — over-limit — rather than
        letting the request through. An unavailable limiter is not an absent one:
        failing open here would remove the only protection against a runaway
        script hammering the endpoint, at exactly the moment we cannot see what
        is happening.
        """
        ttl = int(self._capacity / max(self._refill, 0.001)) + 60
        try:
            result = await self._script(
                keys=[f"sg:ratelimit:{endpoint_hash}"],
                args=[self._capacity, self._refill, now_ms, ttl],
            )
        except RedisError as exc:
            logger.error("Rate limiter unavailable", extra={"error": type(exc).__name__})
            return False
        return bool(int(result[0]))
