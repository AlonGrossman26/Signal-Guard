"""Enumerations shared across layers.

These are plain string enums with no I/O and no dependencies, so `risk/` can
import them without breaking its purity rule.

Values are persisted to the database as TEXT, so **renaming a member is a
migration, not a refactor** — old rows keep the old string forever.
"""

from __future__ import annotations

from enum import StrEnum


class Verdict(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReasonCode(StrEnum):
    """Why a decision came out the way it did.

    Two families, deliberately kept distinct (plan §4, OQ-1):

    * **Rule codes** are produced by the pure risk engine, in the order given in
      CLAUDE.md §7. Numbering below matches that table.
    * **Pipeline codes** are produced by the orchestration layer *around* the
      engine, for failures that occur before a complete snapshot can be built —
      the broker is unreachable, Redis is down, the account does not exist. The
      engine never runs in those cases, but a decision row is still written,
      because constraint #5 requires every alert to have an auditable outcome.

    Folding the second family into INVALID_PAYLOAD would make the dashboard's
    rejection breakdown lie about why trades were refused.
    """

    # Approved.
    APPROVED = "APPROVED"

    # --- Rule family: CLAUDE.md §7, in evaluation order -----------------------
    TRADING_LOCKED = "TRADING_LOCKED"              # 1  kill switch / account lock
    INVALID_PAYLOAD = "INVALID_PAYLOAD"            # 2  schema invalid or unparseable
    STALE_ALERT = "STALE_ALERT"                    # 3  too old, or too far in the future
    DUPLICATE_ALERT = "DUPLICATE_ALERT"            # 4  dedupe_key seen inside the window
    SYMBOL_NOT_ALLOWED = "SYMBOL_NOT_ALLOWED"      # 5  not on the user's allowlist
    NO_STOP_LOSS = "NO_STOP_LOSS"                  # 6  missing, zero, or wrong-side stop
    CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"  # 7  consecutive losses, cooling down
    DAILY_DRAWDOWN_HIT = "DAILY_DRAWDOWN_HIT"      # 8  daily loss limit reached
    SIZE_BELOW_MINIMUM = "SIZE_BELOW_MINIMUM"      # 9  computed qty under exchange minimum
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"              # 10 too many/too large open positions

    # --- Pipeline family: fail-closed, engine never ran (OQ-1) ----------------
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"          # no account state -> no snapshot
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"            # Redis down; lock state unknowable
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"            # payload named an unknown account
    INSTRUMENT_UNAVAILABLE = "INSTRUMENT_UNAVAILABLE"  # no fresh exchange filters
    INTERNAL_ERROR = "INTERNAL_ERROR"                  # anything unexpected — still rejects

    @property
    def is_pipeline_failure(self) -> bool:
        """True for codes raised outside the pure engine."""
        return self in _PIPELINE_CODES


_PIPELINE_CODES = frozenset(
    {
        ReasonCode.BROKER_UNAVAILABLE,
        ReasonCode.STATE_UNAVAILABLE,
        ReasonCode.ACCOUNT_NOT_FOUND,
        ReasonCode.INSTRUMENT_UNAVAILABLE,
        ReasonCode.INTERNAL_ERROR,
    }
)


class TradingState(StrEnum):
    """Durable per-account trading state. LOCKED is the kill switch (plan §3, Q1)."""

    ACTIVE = "ACTIVE"
    LOCKED = "LOCKED"


class CircuitState(StrEnum):
    CLOSED = "CLOSED"  # normal
    OPEN = "OPEN"      # tripped, blocking new entries


class ParseStatus(StrEnum):
    """How far an inbound alert got before we gave up on it."""

    OK = "OK"
    INVALID_JSON = "INVALID_JSON"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    OVERSIZED = "OVERSIZED"
    AUTH_FAILED = "AUTH_FAILED"


class AuthMode(StrEnum):
    """Recorded per alert so the weaker mode is visible in the audit trail."""

    HMAC = "HMAC"                # signature over the raw body — preferred
    BODY_SECRET = "BODY_SECRET"  # shared secret in the JSON — TradingView fallback


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderRole(StrEnum):
    """What an order is *for*.

    Needed to tell an entry from its protective stop, which "never leave a naked
    position" (CLAUDE.md §10) depends on being able to distinguish.
    """

    ENTRY = "ENTRY"
    STOP = "STOP"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"
    KILL_SWITCH = "KILL_SWITCH"


class OrderStatus(StrEnum):
    # Written BEFORE the HTTP call to the broker, so a lost response is still
    # recoverable: we always know what we sent and under which client_order_id.
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # broker did not answer; reconciliation must resolve it


class AlertAction(StrEnum):
    """Webhook `action` values.

    On Binance spot there is no shorting (plan §4, OQ-6): BUY opens or increases
    a long, SELL reduces or closes one, CLOSE flattens the symbol entirely.
    """

    BUY = "buy"
    SELL = "sell"
    CLOSE = "close"
