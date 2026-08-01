"""End-to-end: a live webhook alert publishes a decision to the user's channel.

This proves the wiring, not just the publisher: a real POST to the webhook
endpoint runs the background risk evaluation, persists the decision, and fans it
out over Redis. The dashboard's headline feature — the live decision feed —
depends on exactly this path.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from signalguard.realtime import channel_for
from signalguard.redis_client import get_redis
from tests.api.conftest import register

pytestmark = pytest.mark.integration


async def test_live_webhook_publishes_a_decision_event(client: AsyncClient) -> None:
    await register(client)
    user_id = uuid.UUID((await client.get("/api/auth/me")).json()["id"])

    # Create the account the alert routes to, and a webhook endpoint to post at.
    await client.post(
        "/api/broker-accounts",
        json={"label": "binance-testnet-1", "api_key": "k", "api_secret": "s"},
    )
    endpoint = (await client.post("/api/webhook-endpoints")).json()
    token = endpoint["endpoint_token"]
    body_secret = endpoint["body_secret"]

    # Subscribe before posting so the published event cannot be missed.
    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel_for(user_id))

    body = json.dumps(
        {
            "secret": body_secret,
            "id": f"signal-{uuid.uuid4()}",
            "timestamp": datetime.now(UTC).isoformat(),
            "account": "binance-testnet-1",
            "symbol": "BTCUSDT",
            "action": "buy",
            "order_type": "limit",
            "limit_price": "62000.00",
            "stop_price": "61000.00",
        }
    ).encode()

    response = await client.post(
        f"/webhook/{token}", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 200

    received = None
    for _ in range(50):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message is not None:
            received = json.loads(message["data"])
            break
    await pubsub.unsubscribe(channel_for(user_id))
    await pubsub.aclose()

    assert received is not None, "no decision event was published"
    assert received["type"] == "decision"
    # A verdict was reached and fanned out, whatever it turned out to be.
    assert received["data"]["reason_code"]
