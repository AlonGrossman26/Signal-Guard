"""A minimal Telegram Bot API client for sending messages.

Scope is deliberately one method: send a text message to one chat. Everything
this product needs to tell a user — a kill switch fired, a position was left
naked, the circuit breaker opened — is a short message, and a bigger client
would be surface area with no purpose (CLAUDE.md §13: build exactly what v1
needs).

Two constraints shape it:

* **The bot token is a secret** (constraint #6). It appears only in the request
  URL, never in a log line or an exception message. Errors are reported by HTTP
  status and our own text, never by echoing the request.
* **Sending is idempotent enough to retry.** Re-sending a duplicate alert is
  harmless (a second "kill switch fired" line is noise, not danger), so transient
  failures — network errors, 5xx, 429 — are retried with backoff. A 4xx that is
  not 429 is a permanent problem (bad token, bad chat id) and is not retried.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT_SEC = 10.0
MAX_RETRIES = 3
# Telegram caps message text at 4096 chars; keep a margin and truncate rather
# than let the API reject the whole message.
MAX_TEXT_LEN = 4000


class TelegramError(Exception):
    """A send failed after retries. Carries no secret and no raw provider text."""


class TelegramClient:
    """Sends messages to one chat via one bot. Reusable across sends."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = TELEGRAM_API_BASE,
    ) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id
        self._base_url = base_url
        # An injected client is used as-is (tests pass a mock transport); an owned
        # one is created lazily so constructing the client opens no sockets.
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, text: str, *, parse_mode: str = "HTML") -> None:
        """Send a message, retrying transient failures. Raises TelegramError on
        permanent failure or exhausted retries — the caller decides what to do
        (the Notifier swallows it; a direct caller may not)."""
        # The token is in the path only, never logged.
        url = f"{self._base_url}/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text[:MAX_TEXT_LEN],
            "parse_mode": parse_mode,
            # Alerts are self-contained; link previews would be noise.
            "disable_web_page_preview": True,
        }

        client = await self._http()
        last_detail = "unknown error"
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                # Network-level failure: transient, worth another try.
                last_detail = f"transport error: {type(exc).__name__}"
                await self._backoff(attempt)
                continue

            if response.status_code == 200:
                return
            if response.status_code == 429 or response.status_code >= 500:
                # Rate limited or server-side: back off and retry.
                last_detail = f"http {response.status_code}"
                await self._backoff(attempt)
                continue
            # A permanent client error (bad token, bad chat, malformed request).
            # Do not retry, and do not include the body — it can echo the token.
            raise TelegramError(f"telegram rejected the message: http {response.status_code}")

        raise TelegramError(f"telegram send failed after {MAX_RETRIES} attempts: {last_detail}")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        # 0.5s, 1s, 2s — bounded, so a flaky Telegram never stalls a caller for
        # long. The final attempt does not sleep afterwards (the loop ends).
        if attempt < MAX_RETRIES:
            await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
