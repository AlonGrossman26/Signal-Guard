"""Webhook authentication (CLAUDE.md §8).

Two modes, and the difference between them is recorded on every alert so it is
visible in the audit trail rather than buried in configuration:

* **HMAC** (preferred) — a signature over the raw body plus a timestamp header
  for replay protection. A captured request is useless after the replay window.
* **Body secret** (fallback) — a shared secret inside the JSON. Weaker, because
  a captured request replays forever and the secret is in the payload itself. It
  exists only because TradingView's free plan cannot send custom headers, and
  refusing to support it would exclude most of the intended users.

The UI must label the second mode as weaker. Supporting it quietly, as though the
two were equivalent, would be the dishonest choice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.config import Settings
from signalguard.crypto import (
    constant_time_compare,
    decrypt_credential,
    hash_endpoint_id,
    verify_hmac,
)
from signalguard.db.models import WebhookEndpoint
from signalguard.enums import AuthMode

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Signature"
TIMESTAMP_HEADER = "X-Signature-Timestamp"

# A signed request older than this is refused even with a valid signature, so a
# captured request cannot be replayed indefinitely.
REPLAY_WINDOW_SEC = 300


@dataclass(frozen=True)
class AuthResult:
    endpoint: WebhookEndpoint
    mode: AuthMode
    signature_valid: bool


class AuthError(Exception):
    """Authentication failed. The message is deliberately non-specific."""


async def resolve_endpoint(
    session: AsyncSession, endpoint_id: str, settings: Settings
) -> WebhookEndpoint:
    """Look up an endpoint by its token.

    The token is hashed with the server pepper before the lookup, so the raw
    value never touches a query, a log, or an index.
    """
    endpoint_hash = hash_endpoint_id(endpoint_id, settings.endpoint_id_pepper)
    result = await session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.endpoint_id_hash == endpoint_hash,
            WebhookEndpoint.is_active.is_(True),
        )
    )
    endpoint = result.scalar_one_or_none()
    if endpoint is None:
        # Same message whether the endpoint is unknown, inactive, or belongs to
        # someone else: an attacker probing URLs learns nothing from the reply.
        raise AuthError("unknown or inactive endpoint")
    return endpoint


def _check_replay_window(timestamp_header: str | None, now: datetime) -> None:
    if not timestamp_header:
        raise AuthError("missing signature timestamp")
    try:
        sent_at = datetime.fromisoformat(timestamp_header.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthError("malformed signature timestamp") from exc

    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)

    if abs(now - sent_at) > timedelta(seconds=REPLAY_WINDOW_SEC):
        raise AuthError("signature timestamp outside the replay window")


def authenticate(
    *,
    endpoint: WebhookEndpoint,
    raw_body: bytes,
    signature: str | None,
    signature_timestamp: str | None,
    body_secret: str | None,
    settings: Settings,
    now: datetime,
) -> AuthResult:
    """Verify a request, preferring HMAC and falling back to the body secret."""
    if signature:
        _check_replay_window(signature_timestamp, now)
        hmac_secret = decrypt_credential(
            endpoint.hmac_secret_encrypted,
            endpoint.hmac_secret_nonce,
            settings.credentials_master_key,
        )
        if not verify_hmac(raw_body, hmac_secret, signature):
            raise AuthError("invalid signature")
        return AuthResult(endpoint=endpoint, mode=AuthMode.HMAC, signature_valid=True)

    if body_secret:
        expected = decrypt_credential(
            endpoint.body_secret_encrypted,
            endpoint.body_secret_nonce,
            settings.credentials_master_key,
        )
        if not constant_time_compare(body_secret, expected):
            raise AuthError("invalid secret")
        return AuthResult(
            endpoint=endpoint, mode=AuthMode.BODY_SECRET, signature_valid=True
        )

    raise AuthError("no credentials supplied")
