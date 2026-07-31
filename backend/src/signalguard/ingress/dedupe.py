"""Duplicate detection (CLAUDE.md §7 rule 4, §8).

Two layers, deliberately:

* **Redis** owns the time window. `SET key value NX EX window` is atomic, so
  concurrent deliveries of the same alert cannot both win the race — exactly one
  gets the key.
* **Postgres** owns the durable guarantee, via the partial unique index on
  `decisions.alert_id`. Redis can be flushed or restarted; the database cannot
  quietly forget that a decision already exists.

Redis alone would be a cache pretending to be a guarantee. Postgres alone would
be correct but too slow for the <50 ms budget, and could not express "within the
last 60 seconds" cheaply. Neither layer is redundant.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_KEY_PREFIX = "sg:dedupe:"


class StateUnavailableError(RuntimeError):
    """Redis could not answer. The caller must fail closed."""


def compute_dedupe_key(
    endpoint_id: str,
    signal_id: str | None,
    symbol: str,
    action: str,
    timestamp: datetime,
) -> str:
    """Derive the dedupe key (CLAUDE.md §8).

    Prefers the sender's own `id`. Without one, falls back to the signal's
    identity rounded to the second — because a retry milliseconds later is the
    same signal, while a genuinely new signal for the same symbol and side within
    the same second is not something a strategy does on purpose.

    The endpoint is always part of the hash, so two users cannot collide.
    """
    if signal_id:
        material = f"{endpoint_id}:{signal_id}"
    else:
        rounded = timestamp.replace(microsecond=0).isoformat()
        material = f"{endpoint_id}:{symbol}:{action}:{rounded}"
    return hashlib.sha256(material.encode()).hexdigest()


async def claim_dedupe_key(redis: Redis, dedupe_key: str, window_sec: int) -> bool:
    """Atomically claim a key. True if this is the first sighting.

    Raises `StateUnavailableError` when Redis is unreachable. It deliberately
    does not return "not a duplicate" on failure: an unknown answer must not be
    read as permission, or a Redis outage would turn every retry into a second
    order (plan §3, Q5).
    """
    try:
        claimed = await redis.set(
            f"{_KEY_PREFIX}{dedupe_key}", "1", nx=True, ex=window_sec
        )
    except RedisError as exc:
        logger.error("Dedupe check failed", extra={"error": type(exc).__name__})
        raise StateUnavailableError("Redis unavailable for dedupe") from exc
    return bool(claimed)


async def release_dedupe_key(redis: Redis, dedupe_key: str) -> None:
    """Release a claim so the alert can be retried.

    Used when processing fails *before* a decision is persisted. Without this, a
    transient failure would leave the key claimed and the sender's retry would be
    rejected as a duplicate — turning a recoverable blip into a lost signal.
    """
    try:
        await redis.delete(f"{_KEY_PREFIX}{dedupe_key}")
    except RedisError:
        # Best effort. The key expires on its own, and a stale claim is a missed
        # trade rather than a duplicate one — the safe direction to fail.
        logger.warning("Could not release dedupe key")
