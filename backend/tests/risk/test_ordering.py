"""Rule ordering (CLAUDE.md §13): several violations return the highest-priority code.

Order is part of the specification. When a payload breaks four rules, the user's
dashboard must show them the most important thing that was wrong — not whichever
check happened to run first. These tests pin that ordering so a future
refactor cannot quietly reshuffle it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from signalguard.enums import CircuitState, ReasonCode, TradingState
from signalguard.risk import evaluate
from signalguard.risk.rules import ORDERED_RULES
from signalguard.risk.types import CircuitBreakerSnapshot, DrawdownSnapshot
from tests.risk.builders import (
    BTCUSDT,
    EQUITY,
    NOW,
    account,
    alert,
    config,
    position,
    snapshot,
)

# Every violation, worst-first. Each entry adds its own breakage on top of the
# previous ones, so evaluating entry N means rules 1..N are all violated.
VIOLATIONS: list[tuple[ReasonCode, dict[str, object]]] = [
    (ReasonCode.TRADING_LOCKED, {"user_trading_locked": True}),
    (ReasonCode.INVALID_PAYLOAD, {"alert": None, "payload_error": "garbage"}),
    (ReasonCode.STALE_ALERT, {"stale": True}),
    (ReasonCode.DUPLICATE_ALERT, {"is_duplicate": True}),
    (ReasonCode.SYMBOL_NOT_ALLOWED, {"deny_symbol": True}),
    (ReasonCode.NO_STOP_LOSS, {"no_stop": True}),
    (ReasonCode.CIRCUIT_BREAKER_OPEN, {"breaker_open": True}),
    (ReasonCode.DAILY_DRAWDOWN_HIT, {"drawdown_hit": True}),
    (ReasonCode.SIZE_BELOW_MINIMUM, {"size_too_small": True}),
    (ReasonCode.EXPOSURE_LIMIT, {"exposure_hit": True}),
]


def _build(flags: dict[str, object]) -> dict[str, object]:
    """Translate violation flags into snapshot overrides."""
    overrides: dict[str, object] = {}
    alert_kwargs: dict[str, object] = {}
    config_kwargs: dict[str, object] = {}
    account_kwargs: dict[str, object] = {}

    if flags.get("user_trading_locked"):
        overrides["user_trading_locked"] = True
    if "alert" in flags:
        overrides["alert"] = flags["alert"]
        overrides["payload_error"] = flags.get("payload_error")
    if flags.get("stale"):
        alert_kwargs["timestamp"] = NOW - timedelta(seconds=120)
    if flags.get("is_duplicate"):
        overrides["is_duplicate"] = True
    if flags.get("deny_symbol"):
        config_kwargs["allowed_symbols"] = frozenset()
    if flags.get("no_stop"):
        alert_kwargs["stop_price"] = None
    if flags.get("breaker_open"):
        overrides["circuit"] = CircuitBreakerSnapshot(
            state=CircuitState.OPEN,
            consecutive_losses=5,
            cooldown_until=NOW + timedelta(hours=1),
        )
    if flags.get("drawdown_hit"):
        overrides["drawdown"] = DrawdownSnapshot(
            baseline_equity=EQUITY, tripped_today=True
        )
    if flags.get("size_too_small"):
        # Raise the exchange minimum rather than crushing equity. Dropping equity
        # to near zero would trip rule 8 (a ~100% drawdown) before sizing ever
        # ran — correct behaviour, but it would stop this flag isolating rule 9.
        overrides["instrument"] = replace(BTCUSDT, min_qty=Decimal("100"))
    if flags.get("exposure_hit"):
        account_kwargs["positions"] = (position(),)

    if alert_kwargs and "alert" not in overrides:
        overrides["alert"] = alert(**alert_kwargs)
    if config_kwargs:
        overrides["config"] = config(**config_kwargs)
    if account_kwargs:
        overrides["account"] = account(**account_kwargs)
    return overrides


@pytest.mark.parametrize(
    ("index", "expected"),
    [(i, code) for i, (code, _) in enumerate(VIOLATIONS)],
    ids=[code.value for code, _ in VIOLATIONS],
)
def test_highest_priority_violation_wins(index: int, expected: ReasonCode) -> None:
    """Violating rules 1..N returns rule 1's code — the most severe."""
    flags: dict[str, object] = {}
    for _, violation in VIOLATIONS[index::-1]:
        flags.update(violation)

    decision = evaluate(snapshot(**_build(flags)))
    assert ReasonCode(decision.reason_code) is VIOLATIONS[0][0] if index >= 0 else True


def test_each_violation_alone_produces_its_own_code() -> None:
    """The setup above is only meaningful if each flag really triggers its rule."""
    for expected, violation in VIOLATIONS:
        decision = evaluate(snapshot(**_build(dict(violation))))
        assert ReasonCode(decision.reason_code) is expected, (
            f"expected {expected} alone, got {decision.reason_code}: "
            f"{decision.reason_detail}"
        )


def test_locked_account_outranks_every_other_violation() -> None:
    """The most absolute check. Nothing else matters if trading is locked."""
    everything_wrong = snapshot(
        user_trading_locked=True,
        is_duplicate=True,
        alert=alert(timestamp=NOW - timedelta(hours=1), stop_price=None),
        config=config(allowed_symbols=frozenset()),
        account=account(trading_state=TradingState.LOCKED, positions=(position(),)),
        drawdown=DrawdownSnapshot(baseline_equity=EQUITY, tripped_today=True),
    )
    assert (
        ReasonCode(evaluate(everything_wrong).reason_code) is ReasonCode.TRADING_LOCKED
    )


def test_stale_outranks_duplicate() -> None:
    """Rule 3 before rule 4 — an adjacent pair, where a reshuffle is most likely."""
    both = snapshot(
        alert=alert(timestamp=NOW - timedelta(seconds=300)), is_duplicate=True
    )
    assert ReasonCode(evaluate(both).reason_code) is ReasonCode.STALE_ALERT


def test_symbol_outranks_stop_loss() -> None:
    both = snapshot(
        config=config(allowed_symbols=frozenset()), alert=alert(stop_price=None)
    )
    assert ReasonCode(evaluate(both).reason_code) is ReasonCode.SYMBOL_NOT_ALLOWED


def test_sizing_outranks_exposure() -> None:
    """Rule 9 before rule 10 — a below-minimum size is reported over a cap breach."""
    both = snapshot(
        instrument=replace(BTCUSDT, min_qty=Decimal("100")),
        account=account(positions=(position(symbol="ETHUSDT"),)),
        config=config(max_open_positions=1),
    )
    assert ReasonCode(evaluate(both).reason_code) is ReasonCode.SIZE_BELOW_MINIMUM


def test_rule_chain_has_exactly_ten_rules() -> None:
    """CLAUDE.md §7 defines ten. An eleventh added without a spec change is a bug."""
    assert len(ORDERED_RULES) == 10


def test_rule_chain_order_matches_the_specification() -> None:
    """The function names, in order, pinned against the spec table."""
    assert [rule.__name__ for rule in ORDERED_RULES] == [
        "rule_trading_locked",   # 1
        "rule_payload_valid",    # 2
        "rule_not_stale",        # 3
        "rule_not_duplicate",    # 4
        "rule_symbol_allowed",   # 5
        "rule_stop_loss",        # 6
        "rule_circuit_breaker",  # 7
        "rule_daily_drawdown",   # 8
        "rule_position_size",    # 9
        "rule_exposure",         # 10
    ]
