"""The realtime publisher: payload shape, a real Redis round-trip, fail-soft."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from signalguard.realtime import EventType, channel_for, encode_event, publish

pytestmark = pytest.mark.integration


def test_encode_event_stringifies_money_and_ids() -> None:
    event = encode_event(
        EventType.DECISION,
        {"computed_qty": Decimal("0.001"), "verdict": "APPROVED", "n": 3},
    )
    decoded = json.loads(event)
    assert decoded["type"] == "decision"
    # Decimal became a string, not a float — constraint #3 on the wire too.
    assert decoded["data"]["computed_qty"] == "0.001"
    assert decoded["data"]["n"] == 3


async def test_publish_round_trips_through_real_redis() -> None:
    import os

    from redis.asyncio import Redis

    redis = Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True
    )
    user_id = uuid.uuid4()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel_for(user_id))
    # Let the subscription register before publishing, or the event is missed.
    await asyncio.sleep(0.1)

    await publish(redis, user_id, EventType.DECISION, {"reason_code": "APPROVED"})

    received = None
    for _ in range(50):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if message is not None:
            received = json.loads(message["data"])
            break
    await pubsub.unsubscribe(channel_for(user_id))
    await pubsub.aclose()
    await redis.aclose()

    assert received is not None
    assert received["type"] == "decision"
    assert received["data"]["reason_code"] == "APPROVED"


async def test_publish_swallows_a_redis_failure() -> None:
    """A pub/sub outage must never bubble up and roll back the write above it."""

    class BrokenRedis:
        async def publish(self, *_: Any, **__: Any) -> int:
            from redis.exceptions import RedisError

            raise RedisError("down")

    # Must not raise.
    await publish(BrokenRedis(), uuid.uuid4(), EventType.ORDER, {"status": "FILLED"})  # type: ignore[arg-type]
