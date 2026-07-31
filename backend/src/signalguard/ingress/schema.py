"""Strict webhook payload parsing (CLAUDE.md §8).

Two things here are load-bearing:

**Money never passes through a float.** The JSON is parsed with
`parse_float=Decimal`, so a number literal like `62000.10` becomes an exact
`Decimal` directly from its text. Standard `json.loads` would produce a float
first, and by then the precision is gone — no amount of later `Decimal()`
wrapping brings it back. This lets us accept both `"62000.10"` and `62000.10`,
which matters because TradingView alert templates emit unquoted numbers
constantly, and rejecting those would make the product unusable for its primary
audience while looking like a principled stand.

**Unknown fields are rejected.** A payload containing `qty` or `leverage` is not
a slightly-wrong payload we should interpret generously — it is a sender that
believes it is controlling something we are ignoring. Silently dropping a field
the sender thought was meaningful is exactly the "assume and proceed" that
constraint #1 forbids.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from signalguard.enums import AlertAction, OrderType

# Bodies larger than this are rejected before parsing (CLAUDE.md §8). A webhook
# alert is a few hundred bytes; anything approaching this is either broken or
# hostile, and parsing it first would be doing an attacker's work for them.
MAX_BODY_BYTES = 16 * 1024


class PayloadError(ValueError):
    """The payload could not be parsed or violated the schema."""


def loads_decimal(raw: bytes | str) -> Any:
    """Parse JSON with every number as an exact Decimal."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)


class WebhookPayload(BaseModel):
    """The one accepted payload shape. Anything else is rejected."""

    # forbid: unknown fields are an error, not something to ignore.
    model_config = ConfigDict(extra="forbid", frozen=True)

    secret: str | None = None
    id: str | None = Field(default=None, max_length=128)
    timestamp: datetime
    account: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    action: AlertAction
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None

    @field_validator("action", "order_type", mode="before")
    @classmethod
    def _normalise_enum_case(cls, value: Any) -> Any:
        """Accept the wire format from CLAUDE.md §8 regardless of case.

        The documented contract is lowercase (`"buy"`, `"market"`), while the
        internal enums use the exchange convention for order types (uppercase).
        Normalising here keeps the wire format exactly as documented without
        forcing that choice on the rest of the codebase — and accepting either
        case costs nothing, since there is no ambiguity to resolve.
        """
        if not isinstance(value, str):
            return value
        lowered = value.strip().lower()
        if lowered in ("market", "limit"):
            return lowered.upper()
        return lowered

    @field_validator("timestamp")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """A naive timestamp is ambiguous, and ambiguity means reject.

        "Assume UTC" would silently mis-age every alert from a sender in another
        zone — by hours, in the direction that makes stale alerts look fresh.
        """
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @field_validator("limit_price", "stop_price", "take_profit")
    @classmethod
    def _prices_positive(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("price fields must be positive")
        return value

    @field_validator("symbol")
    @classmethod
    def _normalise_symbol(cls, value: str) -> str:
        """Exchanges use uppercase symbols; accept either and normalise."""
        return value.upper().strip()


def parse_payload(raw_body: bytes) -> WebhookPayload:
    """Parse and validate a raw request body, or raise `PayloadError`.

    Every failure path raises the same exception type with a safe message, so a
    caller cannot accidentally distinguish "bad JSON" from "bad schema" in a way
    that leaks structure to an attacker probing the endpoint.
    """
    if len(raw_body) > MAX_BODY_BYTES:
        raise PayloadError(f"body exceeds {MAX_BODY_BYTES} bytes")

    try:
        data = loads_decimal(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError, InvalidOperation) as exc:
        raise PayloadError("body is not valid JSON") from exc

    if not isinstance(data, dict):
        raise PayloadError("body must be a JSON object")

    try:
        return WebhookPayload.model_validate(data)
    except ValidationError as exc:
        # Field names and messages only — never the submitted values, which
        # include the shared secret.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        )
        raise PayloadError(problems) from exc


def redact_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Replace the shared secret before the payload is stored or logged.

    CLAUDE.md §6 asks for the payload "verbatim", but constraint #6 says secrets
    are never stored in the clear. In that conflict the secret wins, and
    `alerts.raw_body_sha256` preserves what redaction costs: the ability to prove
    what was actually received (plan §4, OQ-4).
    """
    if "secret" not in data:
        return data
    return {**data, "secret": "[REDACTED]"}
