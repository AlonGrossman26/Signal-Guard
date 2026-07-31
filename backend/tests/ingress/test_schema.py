"""Strict payload parsing (CLAUDE.md §8)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from signalguard.enums import AlertAction, OrderType
from signalguard.ingress.schema import (
    MAX_BODY_BYTES,
    PayloadError,
    parse_payload,
    redact_payload,
)


def body(**overrides: object) -> bytes:
    payload = {
        "secret": "shared-secret",
        "id": "signal-123",
        "timestamp": "2026-07-31T10:15:00Z",
        "account": "binance-testnet-1",
        "symbol": "BTCUSDT",
        "action": "buy",
        "order_type": "market",
        "stop_price": "61000.00",
    }
    payload.update(overrides)  # type: ignore[arg-type]
    return json.dumps(payload).encode()


def test_valid_payload_parses() -> None:
    payload = parse_payload(body())
    assert payload.symbol == "BTCUSDT"
    assert payload.action is AlertAction.BUY
    assert payload.order_type is OrderType.MARKET
    assert payload.stop_price == Decimal("61000.00")


# --- Constraint #3: money never becomes a float -------------------------------


def test_quoted_price_is_an_exact_decimal() -> None:
    payload = parse_payload(body(stop_price="61000.10"))
    assert payload.stop_price == Decimal("61000.10")


def test_unquoted_number_is_also_exact() -> None:
    """TradingView templates emit unquoted numbers constantly.

    Rejecting them would be principled and useless. Parsing with
    `parse_float=Decimal` means the number never becomes a float, so both forms
    are exact.
    """
    raw = b'{"secret":"s","timestamp":"2026-07-31T10:15:00Z","account":"a",' \
          b'"symbol":"BTCUSDT","action":"buy","stop_price":61000.10}'
    payload = parse_payload(raw)
    assert payload.stop_price == Decimal("61000.10")
    assert str(payload.stop_price) == "61000.10"


def test_precision_survives_a_value_a_float_would_mangle() -> None:
    """0.1 + 0.2 float arithmetic is why this matters."""
    raw = b'{"secret":"s","timestamp":"2026-07-31T10:15:00Z","account":"a",' \
          b'"symbol":"BTCUSDT","action":"buy","stop_price":0.089285714285714285}'
    payload = parse_payload(raw)
    assert str(payload.stop_price) == "0.089285714285714285"


# --- Strictness ---------------------------------------------------------------


def test_unknown_field_is_rejected() -> None:
    """A sender controlling a field we ignore is worse than an error."""
    with pytest.raises(PayloadError, match="leverage"):
        parse_payload(body(leverage=10))


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(PayloadError, match="timezone"):
        parse_payload(body(timestamp="2026-07-31T10:15:00"))


def test_missing_required_field_is_rejected() -> None:
    raw = json.dumps({"symbol": "BTCUSDT", "action": "buy"}).encode()
    with pytest.raises(PayloadError):
        parse_payload(raw)


def test_unknown_action_is_rejected() -> None:
    with pytest.raises(PayloadError):
        parse_payload(body(action="short"))


def test_negative_price_is_rejected() -> None:
    with pytest.raises(PayloadError, match="positive"):
        parse_payload(body(stop_price="-100"))


def test_malformed_json_is_rejected() -> None:
    with pytest.raises(PayloadError, match="not valid JSON"):
        parse_payload(b"{not json")


def test_non_object_body_is_rejected() -> None:
    with pytest.raises(PayloadError, match="JSON object"):
        parse_payload(b"[1, 2, 3]")


def test_oversized_body_is_rejected_before_parsing() -> None:
    oversized = b'{"x":"' + b"a" * MAX_BODY_BYTES + b'"}'
    with pytest.raises(PayloadError, match="exceeds"):
        parse_payload(oversized)


def test_symbol_is_normalised_to_uppercase() -> None:
    assert parse_payload(body(symbol="btcusdt")).symbol == "BTCUSDT"


def test_timestamp_offsets_are_honoured() -> None:
    """10:15+02:00 is 08:15 UTC — the offset is applied, not ignored."""
    payload = parse_payload(body(timestamp="2026-07-31T10:15:00+02:00"))
    assert payload.timestamp == datetime(2026, 7, 31, 8, 15, tzinfo=UTC)


@pytest.mark.parametrize(
    ("action", "order_type"),
    [("buy", "market"), ("BUY", "MARKET"), ("Buy", "Market")],
)
def test_enum_fields_accept_either_case(action: str, order_type: str) -> None:
    """The documented wire format is lowercase; accepting either costs nothing."""
    payload = parse_payload(body(action=action, order_type=order_type))
    assert payload.action is AlertAction.BUY
    assert payload.order_type is OrderType.MARKET


# --- Redaction ----------------------------------------------------------------


def test_secret_is_redacted_before_storage() -> None:
    """Constraint #6 outranks "store the payload verbatim" (OQ-4)."""
    redacted = redact_payload({"secret": "live-secret", "symbol": "BTCUSDT"})
    assert redacted["secret"] == "[REDACTED]"
    assert redacted["symbol"] == "BTCUSDT"


def test_redaction_leaves_payloads_without_a_secret_alone() -> None:
    original = {"symbol": "BTCUSDT"}
    assert redact_payload(original) == original


def test_redaction_does_not_mutate_the_original() -> None:
    original = {"secret": "live-secret"}
    redact_payload(original)
    assert original["secret"] == "live-secret"
