"""Request and response models for the dashboard API.

Two conventions run through every model here, both inherited from the hard
constraints:

* **Money and quantities cross the wire as strings, never floats.** A JSON float
  cannot represent 0.1 exactly, and constraint #3 forbids a float ever touching
  a price or a size. Response money fields are typed `str`; request money fields
  reject a JSON float outright rather than silently coercing it.
* **Secrets are write-only.** A broker API secret or webhook signing secret can
  be *set* through a request model, but no response model ever contains one —
  they are returned exactly once, at creation, and never again.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator


def _normalise_email(value: str) -> str:
    """A deliberately small email check: has one '@', non-empty parts, lowercased.

    Not RFC 5322 — full email validation is a rabbit hole and a dependency we do
    not need. The address is lowercased so the `users.email` UNIQUE actually means
    one account per human (`db/models/user.py`).
    """
    email = value.strip().lower()
    local, sep, domain = email.partition("@")
    if not sep or not local or "." not in domain or domain.startswith("."):
        raise ValueError("not a valid email address")
    return email


Email = Annotated[str, Field(max_length=320), BeforeValidator(_normalise_email)]


def _reject_float(value: Any) -> Any:
    """A money field must arrive as a string (or int), never a JSON float.

    Accepting a float here would let 62000.1 become 62000.099999999999 before it
    ever reached a `Decimal`, which is the precise failure constraint #3 exists
    to prevent. int is allowed because it is exact.
    """
    if isinstance(value, float):
        raise ValueError("monetary values must be sent as strings, not floats")
    return value


MoneyIn = Annotated[Decimal, BeforeValidator(_reject_float)]


# --- Auth ---------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: Email
    # 12 is a deliberate floor: short passwords are the single most common way an
    # account like this gets taken over.
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=256)


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime


# --- Risk profile -------------------------------------------------------------


class RiskProfileUpdate(BaseModel):
    """A partial update. Only the fields present are changed.

    Every bound mirrors a database CHECK constraint so a bad value is rejected
    with a readable 422 here instead of an opaque IntegrityError deeper down. The
    database still enforces them — this is the friendly first line, not the only
    one.
    """

    model_config = ConfigDict(extra="forbid")

    max_alert_age_sec: int | None = Field(default=None, gt=0)
    future_tolerance_sec: int | None = Field(default=None, ge=0)
    dedupe_window_sec: int | None = Field(default=None, gt=0)
    allowed_symbols: list[str] | None = None
    min_stop_distance_pct: MoneyIn | None = Field(default=None, gt=0, lt=1)
    consecutive_loss_threshold: int | None = Field(default=None, gt=0)
    circuit_breaker_cooldown_minutes: int | None = Field(default=None, ge=0)
    circuit_breaker_manual_reset: bool | None = None
    max_daily_dd_pct: MoneyIn | None = Field(default=None, gt=0, lt=1)
    risk_per_trade_pct: MoneyIn | None = Field(default=None, gt=0, lt=1)
    fee_slippage_buffer_bps: int | None = Field(default=None, ge=0)
    max_notional_per_trade: MoneyIn | None = Field(default=None, gt=0)
    max_open_positions: int | None = Field(default=None, gt=0)
    max_total_notional: MoneyIn | None = Field(default=None, gt=0)
    allow_pyramiding: bool | None = None
    daily_reset_time: time | None = None
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("allowed_symbols")
    @classmethod
    def _normalise_symbols(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        # Uppercase and de-duplicate: "btcusdt" and "BTCUSDT" are the same market,
        # and the allowlist is compared against an uppercased symbol.
        seen: dict[str, None] = {}
        for raw in value:
            symbol = raw.strip().upper()
            if not symbol:
                raise ValueError("symbol must not be blank")
            if len(symbol) > 32:
                raise ValueError(f"symbol too long: {symbol!r}")
            seen[symbol] = None
        return list(seen)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str | None) -> str | None:
        """Reject an unknown IANA zone up front (constraint #7).

        A bad zone name would only surface later, when the daily-reset baseline is
        computed — the worst possible time to discover it.
        """
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value


class RiskProfileResponse(BaseModel):
    """The full risk profile. Percentages and money render as strings."""

    version: int
    max_alert_age_sec: int
    future_tolerance_sec: int
    dedupe_window_sec: int
    allowed_symbols: list[str]
    min_stop_distance_pct: str
    consecutive_loss_threshold: int
    circuit_breaker_cooldown_minutes: int
    circuit_breaker_manual_reset: bool
    max_daily_dd_pct: str
    risk_per_trade_pct: str
    fee_slippage_buffer_bps: int
    max_notional_per_trade: str
    max_open_positions: int
    max_total_notional: str
    allow_pyramiding: bool
    daily_reset_time: str
    timezone: str
    updated_at: datetime


# --- Broker accounts ----------------------------------------------------------


class BrokerAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: str = Field(default="binance_spot_testnet", max_length=64)
    label: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=1, max_length=512)
    api_secret: str = Field(min_length=1, max_length=512)


class BrokerAccountResponse(BaseModel):
    """Note what is absent: the credentials. They are never returned, ever."""

    id: uuid.UUID
    broker: str
    label: str
    is_testnet: bool
    is_active: bool
    trading_state: str
    locked_at: datetime | None
    locked_reason: str | None
    created_at: datetime


# --- Webhook endpoints --------------------------------------------------------


class WebhookEndpointResponse(BaseModel):
    """Metadata only — never the token or the secrets."""

    id: uuid.UUID
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None


class WebhookEndpointCreated(WebhookEndpointResponse):
    """Returned exactly once, at creation. The only time the caller sees the token.

    The token and secrets are shown here and never again, because we store only a
    hash of the token and encrypted copies of the secrets — we *cannot* show them
    later even if asked, which is the point.
    """

    endpoint_token: str
    hmac_secret: str
    body_secret: str


# --- Read models: decisions, orders, positions, equity ------------------------


class DecisionResponse(BaseModel):
    id: uuid.UUID
    alert_id: uuid.UUID
    broker_account_id: uuid.UUID | None
    verdict: str
    reason_code: str
    reason_detail: str | None
    computed_qty: str | None
    entry_reference_price: str | None
    stop_price: str | None
    evaluated_at: datetime
    latency_ms: int
    is_test: bool


class OrderResponse(BaseModel):
    id: uuid.UUID
    decision_id: uuid.UUID
    symbol: str
    side: str
    type: str
    role: str
    qty: str
    price: str | None
    stop_price: str | None
    status: str
    filled_qty: str
    avg_fill_price: str | None
    fees: str
    submitted_at: datetime | None
    filled_at: datetime | None


class PositionResponse(BaseModel):
    symbol: str
    qty: str
    avg_entry: str
    mark_price: str | None
    unrealized_pnl: str | None
    updated_at: datetime


class EquityPoint(BaseModel):
    equity: str
    free_balance: str | None
    taken_at: datetime
    is_session_baseline: bool


# --- Kill switch --------------------------------------------------------------


class KillSwitchResponse(BaseModel):
    account_id: uuid.UUID
    trading_state: str
    locked_at: datetime | None
    locked_reason: str | None
    orders_cancelled: int
    positions_closed: int
    swept: bool
    errors: list[str]


def money_out(value: Decimal | None) -> str | None:
    """Render a Decimal for the wire without ever going through float.

    `str(Decimal)` is exact; `float(Decimal)` is exactly the bug we are avoiding.
    """
    return None if value is None else str(value)


def parse_money(raw: str) -> Decimal:
    """Parse an inbound money string, rejecting NaN/Infinity that `Decimal` allows."""
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not a valid decimal: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError("value must be finite")
    return value
