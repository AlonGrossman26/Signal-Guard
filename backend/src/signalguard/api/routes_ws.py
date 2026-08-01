"""The dashboard WebSocket (CLAUDE.md §11): a live, per-user event stream.

One endpoint, `/ws`, authenticated by the same session cookie as the REST API.
Once connected it forwards every event published on the user's Redis channel —
decisions, order-status changes, position updates, equity ticks — as they
happen. It is a read-only fan-out: the socket never accepts commands, so a
compromised or buggy client cannot *do* anything through it, only watch.

Fail closed on auth: an unauthenticated or expired session is rejected during the
handshake, before the socket is accepted, so no data ever flows to a connection
we could not identify.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.exceptions import RedisError

from signalguard.api.security import SESSION_COOKIE_NAME, authenticate_token
from signalguard.db.session import get_session
from signalguard.realtime import channel_for
from signalguard.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ws"])

# How long to block waiting for an event before sending a heartbeat instead. The
# heartbeat keeps intermediaries from idling the connection out and is how we
# notice a client that has gone away without a clean close.
_HEARTBEAT_TIMEOUT_SEC = 25.0

# Starlette/WS close code for a policy violation (here: failed authentication).
_WS_POLICY_VIOLATION = 1008


@router.websocket("/ws")
async def dashboard_ws(websocket: WebSocket) -> None:
    """Stream one user's realtime events until they disconnect."""
    token = websocket.cookies.get(SESSION_COOKIE_NAME)

    # Authenticate before accepting: a rejected handshake never becomes an open
    # socket, so an unauthenticated peer receives nothing.
    async for session in get_session():
        user = await authenticate_token(session, token)
        break
    else:  # pragma: no cover - get_session always yields once
        user = None

    if user is None:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    redis = get_redis()
    pubsub = redis.pubsub()
    channel = channel_for(user.id)
    await pubsub.subscribe(channel)
    # Tell the client the stream is live. Until this arrives the subscription may
    # not be registered yet, so a client that waits for it cannot miss an event
    # published immediately after it connects.
    await websocket.send_text(json.dumps({"type": "connected"}))
    logger.info("Dashboard WebSocket connected", extra={"user_id": str(user.id)})

    async def forward() -> None:
        """Send every published event to the client, skipping control frames."""
        async for message in pubsub.listen():
            if message.get("type") == "message":
                # decode_responses=True, so data is already the JSON str the
                # publisher shaped — forward it verbatim.
                await websocket.send_text(message["data"])

    async def heartbeat() -> None:
        """Keep the socket warm so idle intermediaries do not drop it."""
        while True:
            await asyncio.sleep(_HEARTBEAT_TIMEOUT_SEC)
            await websocket.send_text(json.dumps({"type": "heartbeat"}))

    async def watch_disconnect() -> None:
        """Resolve when the client goes away. The socket is read-only, so any
        inbound frame is ignored; a close raises and ends this task."""
        while True:
            await websocket.receive()

    # Run the three concurrently; whichever finishes first (a disconnect, a send
    # failure, a pub/sub error) tears the other two down.
    tasks = [
        asyncio.create_task(forward()),
        asyncio.create_task(heartbeat()),
        asyncio.create_task(watch_disconnect()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (WebSocketDisconnect, RedisError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, WebSocketDisconnect, RedisError, RuntimeError):
                await task
        with suppress(RedisError):
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()  # type: ignore[no-untyped-call]
        logger.info("Dashboard WebSocket closed", extra={"user_id": str(user.id)})
