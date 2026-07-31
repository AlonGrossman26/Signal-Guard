"""Redis client lifecycle.

Redis holds dedupe keys, rate limits, circuit-breaker and kill-switch state, and
the pub/sub feeding the dashboard WebSocket. It is the *fast path* for state
whose durable record lives in Postgres — never the only copy of anything that
matters (plan §3, Q5).
"""

from __future__ import annotations

from redis.asyncio import Redis

_client: Redis | None = None


def init_redis(redis_url: str) -> Redis:
    """Create the process-wide Redis client. Called once, from the app lifespan."""
    global _client
    _client = Redis.from_url(
        redis_url,
        decode_responses=True,
        # Bounded timeouts everywhere: a hung Redis must surface as an error we
        # can fail closed on, not as a request that never returns.
        socket_timeout=3.0,
        socket_connect_timeout=3.0,
        health_check_interval=30,
    )
    return _client


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialised — call init_redis() first.")
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
