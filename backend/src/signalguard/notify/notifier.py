"""Domain-event notifications, formatted and sent fail-soft.

The Notifier turns the handful of events a user must not miss into short Telegram
messages. Which events? The loud ones from CLAUDE.md §10 and §7 — a kill switch
firing, a position left naked, the circuit breaker opening, the daily drawdown
limit tripping. Routine approvals are *not* here: a notification for every trade
trains the user to ignore notifications, and then the one that matters is missed
too.

Every method is best-effort. A send that fails is logged and swallowed, because
the caller is almost always doing something more important than notifying —
firing a kill switch, unwinding a naked position — and that must not be held
hostage to Telegram's availability.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Sequence

from signalguard.config import Settings
from signalguard.notify.telegram import TelegramClient, TelegramError

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, client: TelegramClient) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _safe_send(self, title: str, lines: Sequence[str]) -> bool:
        """Format and send. Returns whether it was delivered; never raises."""
        body = "\n".join(html.escape(line) for line in lines)
        text = f"<b>{html.escape(title)}</b>\n{body}" if body else f"<b>{html.escape(title)}</b>"
        try:
            await self._client.send(text)
            return True
        except TelegramError:
            # Log the failure type, never the message content or credentials.
            logger.warning("Telegram notification failed", extra={"title": title})
            return False

    async def kill_switch_fired(
        self,
        account_label: str,
        *,
        orders_cancelled: int,
        positions_closed: int,
        errors: Sequence[str],
    ) -> bool:
        """The single most important notification this system sends."""
        clean = not errors
        title = "🛑 KILL SWITCH FIRED" if clean else "🛑 KILL SWITCH FIRED — WITH ERRORS"
        lines = [
            f"Account: {account_label}",
            f"Orders cancelled: {orders_cancelled}",
            f"Positions closed: {positions_closed}",
        ]
        if not clean:
            lines.append(f"Errors: {', '.join(errors)}")
            lines.append("The account is LOCKED but may not be flat — the reconciler is retrying.")
        return await self._safe_send(title, lines)

    async def naked_position(self, account_label: str, symbol: str) -> bool:
        """A stop failed to place after an entry filled — the loudest §10 case."""
        return await self._safe_send(
            "⚠️ NAKED POSITION — closing now",
            [
                f"Account: {account_label}",
                f"Symbol: {symbol}",
                "A protective stop could not be placed; the position is being closed.",
            ],
        )

    async def circuit_breaker_open(
        self, account_label: str, consecutive_losses: int
    ) -> bool:
        return await self._safe_send(
            "🔌 Circuit breaker OPEN",
            [
                f"Account: {account_label}",
                f"Consecutive losses: {consecutive_losses}",
                "New entries are blocked until the cooldown elapses or you reset it.",
            ],
        )

    async def undelivered_exit(self, account_label: str, reason_code: str) -> bool:
        """An exit signal was rejected because we could not reach the broker.

        The most user-hostile state this system can produce short of a naked
        position: they asked to get out, we said no, and they are still in.
        Worded so someone reading it on a phone knows they have to act.
        """
        return await self._safe_send(
            "EXIT SIGNAL NOT DELIVERED",
            [
                f"Account: {account_label}",
                f"Reason: {reason_code}",
                "Your close/sell signal was REJECTED because the broker could not "
                "be reached, so no closing order was placed.",
                "You may still be holding this position. Check the exchange "
                "directly and close it by hand if needed.",
            ],
        )

    async def daily_drawdown_hit(self, account_label: str) -> bool:
        return await self._safe_send(
            "📉 Daily drawdown limit hit",
            [
                f"Account: {account_label}",
                "New entries are blocked until the next daily reset, even if equity recovers.",
            ],
        )


def notifier_for_user(
    settings: Settings, user_chat_id: str | None
) -> Notifier | None:
    """Build a Notifier that routes to a specific user's chat (§10, §12).

    Shared bot + per-user chat id: one app-level bot token, and the destination
    is the user's own chat when they have set one, otherwise the app-level chat.
    Returns None when no bot token is configured, or when there is no chat to send
    to at all — an explicit "disabled" rather than a client that can never send.
    """
    if not settings.telegram_bot_token:
        return None
    chat_id = user_chat_id or settings.telegram_chat_id
    if not chat_id:
        return None
    return Notifier(TelegramClient(settings.telegram_bot_token, chat_id))


def notifier_from_settings(settings: Settings) -> Notifier | None:
    """Build a Notifier from the app-level config alone (no per-user routing)."""
    return notifier_for_user(settings, None)
