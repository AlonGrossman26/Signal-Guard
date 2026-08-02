"""The Notifier: message formatting, fail-soft delivery, config gating."""

from __future__ import annotations

import base64
import json
import secrets

import httpx

from signalguard.config import Settings
from signalguard.notify.notifier import (
    Notifier,
    notifier_for_user,
    notifier_from_settings,
)
from signalguard.notify.telegram import TelegramClient


def _notifier_capturing(sent: list[str], *, status: int = 200) -> Notifier:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["text"])
        return httpx.Response(status)

    transport = httpx.MockTransport(handler)
    client = TelegramClient("t:tok", "chat", client=httpx.AsyncClient(transport=transport))
    return Notifier(client)


async def test_kill_switch_message_is_formatted() -> None:
    sent: list[str] = []
    notifier = _notifier_capturing(sent)
    ok = await notifier.kill_switch_fired(
        "binance-testnet-1", orders_cancelled=2, positions_closed=1, errors=[]
    )
    await notifier.aclose()

    assert ok is True
    assert len(sent) == 1
    text = sent[0]
    assert "KILL SWITCH FIRED" in text
    assert "WITH ERRORS" not in text
    assert "binance-testnet-1" in text
    assert "Orders cancelled: 2" in text


async def test_kill_switch_with_errors_changes_title() -> None:
    sent: list[str] = []
    notifier = _notifier_capturing(sent)
    await notifier.kill_switch_fired(
        "acct", orders_cancelled=0, positions_closed=0, errors=["close_pass_1:TIMEOUT"]
    )
    await notifier.aclose()
    assert "WITH ERRORS" in sent[0]
    assert "close_pass_1:TIMEOUT" in sent[0]


async def test_html_is_escaped() -> None:
    sent: list[str] = []
    notifier = _notifier_capturing(sent)
    await notifier.naked_position("acct <b>evil</b>", "BTC<USDT")
    await notifier.aclose()
    # Angle brackets from user data are escaped so they cannot break parse_mode.
    assert "<b>evil</b>" not in sent[0]
    assert "&lt;b&gt;evil&lt;/b&gt;" in sent[0]


async def test_notifier_swallows_a_send_failure() -> None:
    sent: list[str] = []
    # A permanent 400 makes the client raise; the notifier must not.
    notifier = _notifier_capturing(sent, status=400)
    ok = await notifier.kill_switch_fired(
        "acct", orders_cancelled=0, positions_closed=0, errors=[]
    )
    await notifier.aclose()
    assert ok is False  # reported as undelivered, but no exception escaped


def _settings(**overrides: str) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://u@h/db",
        "redis_url": "redis://h:6379/0",
        "credentials_master_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        "endpoint_id_pepper": secrets.token_urlsafe(32),
        "session_secret": secrets.token_urlsafe(32),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_notifier_from_settings_none_when_unconfigured() -> None:
    assert notifier_from_settings(_settings()) is None


def test_notifier_from_settings_built_when_configured() -> None:
    settings = _settings(telegram_bot_token="123:abc", telegram_chat_id="42")
    assert settings.telegram_enabled is True
    assert isinstance(notifier_from_settings(settings), Notifier)


def test_partial_telegram_config_is_treated_as_disabled() -> None:
    # A token without a chat id (or vice versa) is not usable — must be disabled.
    assert notifier_from_settings(_settings(telegram_bot_token="123:abc")) is None
    assert notifier_from_settings(_settings(telegram_chat_id="42")) is None


def test_notifier_for_user_prefers_the_user_chat() -> None:
    settings = _settings(telegram_bot_token="123:abc", telegram_chat_id="app-chat")
    notifier = notifier_for_user(settings, "user-chat-999")
    assert notifier is not None
    # Routes to the user's own chat, not the app-level one.
    assert notifier._client._chat_id == "user-chat-999"


def test_notifier_for_user_falls_back_to_the_app_chat() -> None:
    settings = _settings(telegram_bot_token="123:abc", telegram_chat_id="app-chat")
    notifier = notifier_for_user(settings, None)
    assert notifier is not None
    assert notifier._client._chat_id == "app-chat"


def test_notifier_for_user_none_without_a_bot_token() -> None:
    # No app bot token: even a user chat id cannot produce a notifier.
    assert notifier_for_user(_settings(), "user-chat") is None


def test_notifier_for_user_none_without_any_chat() -> None:
    # Token but no chat anywhere → nowhere to send.
    assert notifier_for_user(_settings(telegram_bot_token="123:abc"), None) is None


async def test_undelivered_exit_tells_the_user_they_may_still_be_holding() -> None:
    """plan §3 Q2's uncomfortable case, made loud rather than hidden.

    Rejecting a close signal is the correct behaviour when the broker is
    unreachable — we cannot place the closing order either, so accepting it
    would be a lie. But the user is left in a position they explicitly asked to
    exit, and they will not learn that from a rejection row on a dashboard they
    are not looking at.
    """
    sent: list[str] = []
    notifier = _notifier_capturing(sent)

    assert await notifier.undelivered_exit("binance-testnet-1", "BROKER_UNAVAILABLE")

    message = sent[0]
    assert "EXIT SIGNAL NOT DELIVERED" in message
    assert "binance-testnet-1" in message
    assert "BROKER_UNAVAILABLE" in message
    # The two things the user has to know: nothing was placed, and they should
    # go and look.
    assert "no closing order was placed" in message
    assert "still be holding" in message
