"""The dashboard WebSocket endpoint.

Uses Starlette's synchronous TestClient because it is the one client here that
speaks the WebSocket protocol (httpx's ASGI transport does not). The client's
context manager runs the real app lifespan, so this also exercises startup
wiring end to end.
"""

from __future__ import annotations

import base64
import secrets
import uuid

import pytest
from redis import Redis as SyncRedis
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from signalguard.realtime import EventType, encode_event
from tests.api.conftest import DATABASE_URL, REDIS_URL

pytestmark = pytest.mark.integration

PASSWORD = "a-perfectly-long-password"


@pytest.fixture()
def ws_client(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    monkeypatch.setenv("ENDPOINT_ID_PEPPER", secrets.token_urlsafe(32))
    monkeypatch.setenv("SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("APP_ENV", "test")

    from signalguard.config import get_settings
    from signalguard.main import create_app

    get_settings.cache_clear()
    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()


def test_unauthenticated_ws_is_rejected(ws_client: TestClient) -> None:
    """No session cookie → the handshake is refused before the socket opens."""
    with pytest.raises(WebSocketDisconnect), ws_client.websocket_connect("/ws"):
        pass


def test_authenticated_ws_receives_published_events(ws_client: TestClient) -> None:
    email = f"{uuid.uuid4()}@example.test"
    reg = ws_client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert reg.status_code == 201
    user_id = uuid.UUID(ws_client.get("/api/auth/me").json()["id"])

    publisher = SyncRedis.from_url(REDIS_URL, decode_responses=True)
    with ws_client.websocket_connect("/ws") as ws:
        # Wait for the "connected" frame: after it, the subscription is live and a
        # single publish cannot be raced.
        assert ws.receive_json()["type"] == "connected"
        publisher.publish(
            f"sg:events:user:{user_id}",
            encode_event(EventType.DECISION, {"reason_code": "APPROVED"}),
        )
        received = ws.receive_json()
        assert received["type"] == "decision"
        assert received["data"]["reason_code"] == "APPROVED"
    publisher.close()


def test_ws_channel_is_scoped_to_the_user(ws_client: TestClient) -> None:
    """An event on another user's channel must never reach this socket."""
    email = f"{uuid.uuid4()}@example.test"
    ws_client.post("/api/auth/register", json={"email": email, "password": PASSWORD})
    my_id = uuid.UUID(ws_client.get("/api/auth/me").json()["id"])
    other_id = uuid.uuid4()

    publisher = SyncRedis.from_url(REDIS_URL, decode_responses=True)
    with ws_client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "connected"
        # Publish to a different user's channel, then to mine. Only mine arrives.
        for _ in range(3):
            publisher.publish(
                f"sg:events:user:{other_id}",
                encode_event(EventType.DECISION, {"reason_code": "LEAKED"}),
            )
        publisher.publish(
            f"sg:events:user:{my_id}",
            encode_event(EventType.DECISION, {"reason_code": "MINE"}),
        )
        received = ws.receive_json()
        assert received["type"] == "decision"
        # The other user's events never reach this socket.
        assert received["data"]["reason_code"] == "MINE"
    publisher.close()
