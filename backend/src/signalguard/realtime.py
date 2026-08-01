"""Realtime event fan-out over Redis pub/sub (CLAUDE.md §5 realtime path).

The dashboard's live feed is fed by Redis pub/sub, not by polling: when a
decision is made or an order changes, the layer that wrote it publishes a small
JSON event onto the owning user's channel, and every WebSocket that user has open
receives it.

This module is a *shared utility*, deliberately dependency-light — it imports
only `redis` and the standard library. That is what lets both the ingress layer
(decisions) and the execution layer (orders, positions, equity) publish without
either importing the API layer, keeping the §5 boundaries intact.

Two rules hold here, both inherited:

* **Money crosses as strings, never floats** (constraint #3). Every `Decimal` in
  an event payload is stringified.
* **Publishing never breaks the writer.** A pub/sub failure must not roll back a
  decision or an order — the durable record in Postgres is the source of truth,
  and the live feed is a convenience on top of it. So publish failures are
  logged and swallowed, never raised.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# One channel per user. A WebSocket only ever subscribes to its own user's
# channel, so one user's events can never leak to another's dashboard.
_CHANNEL_PREFIX = "sg:events:user:"


class EventType(StrEnum):
    DECISION = "decision"
    ORDER = "order"
    POSITION = "position"
    EQUITY = "equity"


def channel_for(user_id: uuid.UUID) -> str:
    return f"{_CHANNEL_PREFIX}{user_id}"


def _json_safe(value: Any) -> Any:
    """Recursively make a payload JSON-serialisable without losing precision."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def encode_event(event_type: EventType, data: dict[str, Any]) -> str:
    """Serialise an event to the exact string a WebSocket will forward verbatim."""
    return json.dumps({"type": event_type.value, "data": _json_safe(data)})


async def publish(
    redis: Redis,
    user_id: uuid.UUID,
    event_type: EventType,
    data: dict[str, Any],
) -> None:
    """Publish one event onto a user's channel. Best-effort — never raises.

    A failure here means the live feed missed a tick; the dashboard reconnects
    and re-fetches from the REST API, and the durable record is untouched. That
    is a strictly better outcome than letting a Redis hiccup abort the write that
    produced the event.
    """
    try:
        await redis.publish(channel_for(user_id), encode_event(event_type, data))
    except RedisError:
        logger.warning(
            "Could not publish realtime event",
            extra={"event_type": event_type.value, "user_id": str(user_id)},
        )
