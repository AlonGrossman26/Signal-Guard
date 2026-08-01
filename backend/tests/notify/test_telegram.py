"""The Telegram client: retries, permanent failures, and secret hygiene.

All against httpx's MockTransport — no network is ever touched (§13).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from signalguard.notify.telegram import TelegramClient, TelegramError

BOT_TOKEN = "123456:super-secret-bot-token"
CHAT_ID = "987654"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the real backoff sleeps so retry tests run instantly."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


def _client(handler: object) -> TelegramClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TelegramClient(
        BOT_TOKEN, CHAT_ID, client=httpx.AsyncClient(transport=transport)
    )


async def test_send_success_shapes_the_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    await client.send("hello")
    await client.aclose()

    assert len(seen) == 1
    request = seen[0]
    # The token lives in the path, and the request targets sendMessage.
    assert request.url.path == f"/bot{BOT_TOKEN}/sendMessage"
    import json

    body = json.loads(request.content)
    assert body["chat_id"] == CHAT_ID
    assert body["text"] == "hello"
    assert body["parse_mode"] == "HTML"


async def test_retries_transient_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    await client.send("hi")  # must not raise
    await client.aclose()
    assert calls["n"] == 2


async def test_retries_on_429() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 2 else httpx.Response(200)

    client = _client(handler)
    await client.send("hi")
    await client.aclose()
    assert calls["n"] == 2


async def test_transport_error_is_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    client = _client(handler)
    await client.send("hi")
    await client.aclose()
    assert calls["n"] == 2


async def test_permanent_4xx_raises_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"description": "bad request"})

    client = _client(handler)
    with pytest.raises(TelegramError):
        await client.send("hi")
    await client.aclose()
    # 4xx (not 429) is permanent — tried exactly once.
    assert calls["n"] == 1


async def test_exhausted_retries_raise_and_never_leak_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    with pytest.raises(TelegramError) as excinfo:
        await client.send("hi")
    await client.aclose()
    # The token must not appear in the error surfaced to callers/logs.
    assert BOT_TOKEN not in str(excinfo.value)
