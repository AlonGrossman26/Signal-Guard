"""Constraint #6: secrets never reach the logs.

These tests exist because redaction is the kind of safeguard everyone believes
is working right up until someone reads a production log.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import pytest

from signalguard.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    configure_logging,
    redact,
    register_secret_value,
)


def _emit(caplog: pytest.LogCaptureFixture, **extra: object) -> dict[str, object]:
    """Log one record through the real filter + formatter, return parsed JSON."""
    logger = logging.getLogger("test.redaction")
    record = logger.makeRecord(
        "test.redaction", logging.INFO, __file__, 1, "a message", (), None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    RedactionFilter().filter(record)
    return json.loads(JsonFormatter().format(record))


def test_secret_named_fields_are_redacted() -> None:
    out = _emit(
        None,  # type: ignore[arg-type]
        api_secret="super-secret-value",
        password="hunter2",
        authorization="Bearer abc123",
        x_signature="deadbeef",
    )
    assert out["api_secret"] == REDACTED
    assert out["password"] == REDACTED
    assert out["authorization"] == REDACTED
    assert out["x_signature"] == REDACTED


def test_innocent_fields_survive() -> None:
    """Over-redaction trains people to distrust the filter. Keep it precise."""
    out = _emit(
        None,  # type: ignore[arg-type]
        key_version=3,
        symbol="BTCUSDT",
        computed_qty="0.089",
    )
    assert out["key_version"] == 3
    assert out["symbol"] == "BTCUSDT"


def test_nested_structures_are_redacted_recursively() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "secret": "shhh",
        "nested": {"api_key": "AKIA...", "qty": "1.5"},
        "items": [{"token": "abc"}, {"side": "BUY"}],
    }
    out = _emit(None, payload=payload)  # type: ignore[arg-type]
    got = out["payload"]
    assert isinstance(got, dict)
    assert got["secret"] == REDACTED
    assert got["symbol"] == "BTCUSDT"
    assert got["nested"]["api_key"] == REDACTED
    assert got["nested"]["qty"] == "1.5"
    assert got["items"][0]["token"] == REDACTED
    assert got["items"][1]["side"] == "BUY"


def test_redact_is_depth_limited() -> None:
    """A self-referencing structure must not turn a log call into a hang."""
    looping: dict[str, object] = {"symbol": "BTCUSDT"}
    looping["self"] = looping
    redact(looping)  # must return rather than recurse forever


def test_registered_secret_scrubbed_from_message() -> None:
    """The backstop: a secret interpolated into a message, not passed as a field."""
    secret = "s3cr3t-master-key-value-long-enough"
    register_secret_value(secret)

    logger = logging.getLogger("test.redaction")
    record = logger.makeRecord(
        "test.redaction", logging.ERROR, __file__, 1,
        "connection failed for %s", (secret,), None,
    )
    RedactionFilter().filter(record)
    out = json.loads(JsonFormatter().format(record))

    assert secret not in out["message"]
    assert REDACTED in out["message"]


def test_registered_secret_scrubbed_from_traceback() -> None:
    """Tracebacks are the most common accidental leak — a DSN with a password."""
    secret = "postgresql+asyncpg://user:hunter2password@db:5432/signalguard"
    register_secret_value(secret)

    logger = logging.getLogger("test.redaction")
    try:
        raise ValueError(f"could not connect to {secret}")
    except ValueError:
        import sys

        record = logger.makeRecord(
            "test.redaction", logging.ERROR, __file__, 1,
            "boom", (), sys.exc_info(),
        )
    RedactionFilter().filter(record)
    out = json.loads(JsonFormatter().format(record))

    assert secret not in json.dumps(out)


def test_decimal_serialises_exactly_not_via_float() -> None:
    """Constraint #3 reaches the logs too: 0.1 must not become 0.1000000000000000055."""
    out = _emit(None, qty=Decimal("0.089285714285714285"))  # type: ignore[arg-type]
    assert out["qty"] == "0.089285714285714285"


def test_configure_logging_installs_filter(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    logging.getLogger("test.configure").info("hello", extra={"api_secret": "leak-me"})
    captured = capsys.readouterr().out
    assert "leak-me" not in captured
    assert REDACTED in captured
    # And it is real JSON, one object per line.
    assert json.loads(captured.strip().splitlines()[-1])["message"] == "hello"
