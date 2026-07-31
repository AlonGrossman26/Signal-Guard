"""Snapshot builders for the risk tests.

Every builder returns a snapshot that would be **approved**, so each test changes
exactly one thing and the reason for a rejection is never in doubt. A test that
has to set up six fields to trigger one rule is a test nobody trusts when it
fails.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from signalguard.enums import AlertAction, OrderType, TradingState
from signalguard.risk.types import (
    AccountState,
    AlertInput,
    CircuitBreakerSnapshot,
    DrawdownSnapshot,
    InstrumentSpec,
    OpenPosition,
    RiskConfig,
    Snapshot,
)

NOW = datetime(2026, 7, 31, 10, 15, 0, tzinfo=UTC)

# The worked example used throughout CLAUDE.md and the Phase 0 plan.
ENTRY = Decimal("62000")
STOP = Decimal("61000")
EQUITY = Decimal("10000")

BTCUSDT = InstrumentSpec(
    symbol="BTCUSDT",
    tick_size=Decimal("0.01"),
    lot_step=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    min_notional=Decimal("10"),
)


def config(**overrides: object) -> RiskConfig:
    base = {
        "version": 1,
        "allowed_symbols": frozenset({"BTCUSDT"}),
        # Generous ceilings by default so exposure never rejects by accident;
        # the exposure tests tighten them explicitly.
        "max_notional_per_trade": Decimal("100000"),
        "max_total_notional": Decimal("1000000"),
    }
    base.update(overrides)
    return RiskConfig(**base)  # type: ignore[arg-type]


def account(**overrides: object) -> AccountState:
    base = {
        "trading_state": TradingState.ACTIVE,
        "total_equity": EQUITY,
        "free_balance": EQUITY,
        "position_value": Decimal("0"),
        "positions": (),
    }
    base.update(overrides)
    return AccountState(**base)  # type: ignore[arg-type]


def alert(**overrides: object) -> AlertInput:
    base = {
        "symbol": "BTCUSDT",
        "action": AlertAction.BUY,
        "order_type": OrderType.MARKET,
        "timestamp": NOW,
        "dedupe_key": "test-dedupe-key",
        "stop_price": STOP,
    }
    base.update(overrides)
    return AlertInput(**base)  # type: ignore[arg-type]


def position(
    symbol: str = "BTCUSDT",
    qty: str = "0.1",
    avg_entry: str = "60000",
    mark_price: str = "62000",
) -> OpenPosition:
    return OpenPosition(
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry=Decimal(avg_entry),
        mark_price=Decimal(mark_price),
    )


def snapshot(**overrides: object) -> Snapshot:
    """A snapshot that evaluates to APPROVED unless a test changes something."""
    base: dict[str, object] = {
        "now": NOW,
        "config": config(),
        "circuit": CircuitBreakerSnapshot(),
        "drawdown": DrawdownSnapshot(baseline_equity=EQUITY),
        "alert": alert(),
        "account": account(),
        "instrument": BTCUSDT,
        "reference_price": ENTRY,
    }
    base.update(overrides)
    return Snapshot(**base)  # type: ignore[arg-type]
