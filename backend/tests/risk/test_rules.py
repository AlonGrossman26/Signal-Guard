"""One test group per rule in CLAUDE.md §7: pass, fail, and the exact boundary.

The boundary cases are the point. Almost every real risk bug lives at "should
this be > or >=?", and a test suite that only checks obvious passes and obvious
failures will not catch a single one of them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from signalguard.enums import (
    AlertAction,
    CircuitState,
    OrderType,
    ReasonCode,
    TradingState,
    Verdict,
)
from signalguard.risk import evaluate
from signalguard.risk.types import CircuitBreakerSnapshot, DrawdownSnapshot
from tests.risk.builders import (
    BTCUSDT,
    ENTRY,
    EQUITY,
    NOW,
    account,
    alert,
    config,
    position,
    snapshot,
)


def reason(**overrides: object) -> ReasonCode:
    return ReasonCode(evaluate(snapshot(**overrides)).reason_code)


def test_baseline_snapshot_is_approved() -> None:
    """Every other test changes one thing from here, so this must pass."""
    decision = evaluate(snapshot())
    assert decision.is_approved, decision.reason_detail


# --- Rule 1: kill switch / trading lock ---------------------------------------


def test_rule1_user_level_lock_rejects() -> None:
    assert reason(user_trading_locked=True) is ReasonCode.TRADING_LOCKED


def test_rule1_account_level_lock_rejects() -> None:
    assert (
        reason(account=account(trading_state=TradingState.LOCKED))
        is ReasonCode.TRADING_LOCKED
    )


def test_rule1_active_account_passes() -> None:
    assert evaluate(snapshot(account=account(trading_state=TradingState.ACTIVE))).is_approved


# --- Rule 2: payload validity -------------------------------------------------


def test_rule2_unparseable_payload_rejects() -> None:
    assert (
        reason(alert=None, payload_error="unknown field 'qty'")
        is ReasonCode.INVALID_PAYLOAD
    )


def test_rule2_naive_timestamp_rejects() -> None:
    """A timestamp without a zone is ambiguous, and ambiguity means reject."""
    assert (
        reason(alert=alert(timestamp=NOW.replace(tzinfo=None)))
        is ReasonCode.INVALID_PAYLOAD
    )


def test_rule2_limit_order_without_price_rejects() -> None:
    assert (
        reason(alert=alert(order_type=OrderType.LIMIT, limit_price=None))
        is ReasonCode.INVALID_PAYLOAD
    )


def test_rule2_non_positive_limit_price_rejects() -> None:
    assert (
        reason(alert=alert(order_type=OrderType.LIMIT, limit_price=Decimal("0")))
        is ReasonCode.INVALID_PAYLOAD
    )


# --- Rule 3: staleness --------------------------------------------------------


def test_rule3_fresh_alert_passes() -> None:
    assert evaluate(snapshot(alert=alert(timestamp=NOW - timedelta(seconds=5)))).is_approved


def test_rule3_old_alert_rejects() -> None:
    assert (
        reason(alert=alert(timestamp=NOW - timedelta(seconds=31)))
        is ReasonCode.STALE_ALERT
    )


def test_rule3_boundary_exactly_at_max_age_passes() -> None:
    """"Older than max_alert_age_sec" means strictly older — 30s exactly is fine."""
    at_limit = snapshot(alert=alert(timestamp=NOW - timedelta(seconds=30)))
    assert evaluate(at_limit).is_approved


def test_rule3_boundary_just_past_max_age_rejects() -> None:
    past = snapshot(alert=alert(timestamp=NOW - timedelta(seconds=30, milliseconds=1)))
    assert ReasonCode(evaluate(past).reason_code) is ReasonCode.STALE_ALERT


def test_rule3_far_future_alert_rejects() -> None:
    """A future-dated alert means a clock is wrong. Refuse rather than guess."""
    assert (
        reason(alert=alert(timestamp=NOW + timedelta(seconds=6)))
        is ReasonCode.STALE_ALERT
    )


def test_rule3_boundary_exactly_at_future_tolerance_passes() -> None:
    at_limit = snapshot(alert=alert(timestamp=NOW + timedelta(seconds=5)))
    assert evaluate(at_limit).is_approved


# --- Rule 4: duplicates -------------------------------------------------------


def test_rule4_duplicate_rejects() -> None:
    assert reason(is_duplicate=True) is ReasonCode.DUPLICATE_ALERT


def test_rule4_non_duplicate_passes() -> None:
    assert evaluate(snapshot(is_duplicate=False)).is_approved


# --- Rule 5: symbol allowlist -------------------------------------------------


def test_rule5_symbol_not_on_list_rejects() -> None:
    assert (
        reason(config=config(allowed_symbols=frozenset({"ETHUSDT"})))
        is ReasonCode.SYMBOL_NOT_ALLOWED
    )


def test_rule5_empty_allowlist_denies_everything() -> None:
    """Default deny: a fresh profile trades nothing until a symbol is opted in."""
    assert (
        reason(config=config(allowed_symbols=frozenset()))
        is ReasonCode.SYMBOL_NOT_ALLOWED
    )


def test_rule5_listed_symbol_passes() -> None:
    listed = snapshot(config=config(allowed_symbols=frozenset({"BTCUSDT", "ETHUSDT"})))
    assert evaluate(listed).is_approved


# --- Rule 6: mandatory stop-loss ----------------------------------------------


def test_rule6_missing_stop_rejects() -> None:
    assert reason(alert=alert(stop_price=None)) is ReasonCode.NO_STOP_LOSS


def test_rule6_zero_stop_rejects() -> None:
    assert reason(alert=alert(stop_price=Decimal("0"))) is ReasonCode.NO_STOP_LOSS


def test_rule6_long_stop_above_entry_rejects() -> None:
    """A long's stop must be below entry, or it triggers instantly at a profit."""
    assert reason(alert=alert(stop_price=Decimal("63000"))) is ReasonCode.NO_STOP_LOSS


def test_rule6_stop_equal_to_entry_rejects() -> None:
    assert reason(alert=alert(stop_price=ENTRY)) is ReasonCode.NO_STOP_LOSS


def test_rule6_boundary_exactly_at_minimum_distance_passes() -> None:
    """min_stop_distance_pct 0.1% of 62000 = 62.00, so a stop at 61938 is exact.

    Asserted as "rule 6 does not fire" rather than "approved", because a stop at
    the minimum legal distance is still a very tight stop, and a tight stop means
    a large position: 100 / (62 + 124) = 0.5376 BTC, about $33,000 of notional
    against a $10,000 balance. So this snapshot passes rule 6 and is then caught
    by the affordability ceiling in rule 10.

    That layering is the design working as intended, and it is worth seeing
    plainly: the stop-distance floor alone does not make a position safe — it
    makes the *size calculation* meaningful, and the exposure caps do the rest.
    """
    exact = evaluate(snapshot(alert=alert(stop_price=Decimal("61938"))))
    assert ReasonCode(exact.reason_code) is not ReasonCode.NO_STOP_LOSS


def test_rule6_boundary_just_inside_minimum_distance_rejects() -> None:
    """One cent closer than the minimum. This is the exploit the rule exists for."""
    too_close = snapshot(alert=alert(stop_price=Decimal("61938.01")))
    assert ReasonCode(evaluate(too_close).reason_code) is ReasonCode.NO_STOP_LOSS


def test_rule6_near_zero_stop_distance_rejects() -> None:
    """One tick from entry would otherwise size an absurdly large position."""
    assert reason(alert=alert(stop_price=Decimal("61999.99"))) is ReasonCode.NO_STOP_LOSS


def test_rule6_exit_needs_no_stop() -> None:
    """You do not attach a protective stop to the order that closes a position."""
    exit_alert = alert(action=AlertAction.CLOSE, stop_price=None)
    decision = evaluate(
        snapshot(alert=exit_alert, account=account(positions=(position(),)))
    )
    assert decision.is_approved


# --- Rule 7: circuit breaker --------------------------------------------------


def test_rule7_open_breaker_within_cooldown_rejects() -> None:
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW + timedelta(minutes=30),
    )
    assert reason(circuit=breaker) is ReasonCode.CIRCUIT_BREAKER_OPEN


def test_rule7_boundary_exactly_at_threshold_rejects() -> None:
    """Threshold 3 means three losses trips it, not four."""
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.CLOSED,
        consecutive_losses=3,
        cooldown_until=NOW + timedelta(minutes=1),
    )
    assert reason(circuit=breaker) is ReasonCode.CIRCUIT_BREAKER_OPEN


def test_rule7_boundary_one_below_threshold_passes() -> None:
    breaker = CircuitBreakerSnapshot(state=CircuitState.CLOSED, consecutive_losses=2)
    assert evaluate(snapshot(circuit=breaker)).is_approved


def test_rule7_elapsed_cooldown_passes() -> None:
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW - timedelta(seconds=1),
    )
    assert evaluate(snapshot(circuit=breaker)).is_approved


def test_rule7_manual_reset_outranks_elapsed_cooldown() -> None:
    """A user who asked to be stopped stays stopped until they say otherwise."""
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW - timedelta(hours=5),
    )
    assert (
        reason(circuit=breaker, config=config(circuit_breaker_manual_reset=True))
        is ReasonCode.CIRCUIT_BREAKER_OPEN
    )


def test_rule7_open_with_no_cooldown_recorded_rejects() -> None:
    """Unknown expiry is not "expired". Fail closed."""
    breaker = CircuitBreakerSnapshot(state=CircuitState.OPEN, consecutive_losses=3)
    assert reason(circuit=breaker) is ReasonCode.CIRCUIT_BREAKER_OPEN


def test_rule7_exit_allowed_while_breaker_open() -> None:
    """The breaker stops new risk; it must not trap you in a position."""
    breaker = CircuitBreakerSnapshot(state=CircuitState.OPEN, consecutive_losses=5)
    decision = evaluate(
        snapshot(
            circuit=breaker,
            alert=alert(action=AlertAction.CLOSE, stop_price=None),
            account=account(positions=(position(),)),
        )
    )
    assert decision.is_approved


# --- Rule 8: daily drawdown ---------------------------------------------------


def test_rule8_within_limit_passes() -> None:
    within = snapshot(account=account(total_equity=Decimal("9800"), free_balance=Decimal("9800")))
    assert evaluate(within).is_approved


def test_rule8_boundary_exactly_at_limit_rejects() -> None:
    """5% of 10,000 is 9,500 — "reaching" the limit rejects, per the >= in §7."""
    at_limit = snapshot(
        account=account(total_equity=Decimal("9500"), free_balance=Decimal("9500"))
    )
    assert ReasonCode(evaluate(at_limit).reason_code) is ReasonCode.DAILY_DRAWDOWN_HIT


def test_rule8_boundary_one_cent_inside_limit_passes() -> None:
    inside = snapshot(
        account=account(total_equity=Decimal("9500.01"), free_balance=Decimal("9500.01"))
    )
    assert evaluate(inside).is_approved


def test_rule8_stays_tripped_after_equity_recovers() -> None:
    """Once hit, the block persists to the next reset even if equity bounces back.

    Without this, a user who breached their daily limit and recovered could keep
    trading — which is exactly what a daily loss limit exists to prevent.
    """
    recovered = snapshot(
        account=account(total_equity=Decimal("10500"), free_balance=Decimal("10500")),
        drawdown=DrawdownSnapshot(baseline_equity=EQUITY, tripped_today=True),
    )
    assert ReasonCode(evaluate(recovered).reason_code) is ReasonCode.DAILY_DRAWDOWN_HIT


def test_rule8_missing_baseline_rejects() -> None:
    """No usable baseline means the limit cannot be evaluated. Fail closed."""
    assert (
        reason(drawdown=DrawdownSnapshot(baseline_equity=Decimal("0")))
        is ReasonCode.DAILY_DRAWDOWN_HIT
    )


def test_rule8_exit_allowed_while_drawdown_hit() -> None:
    decision = evaluate(
        snapshot(
            drawdown=DrawdownSnapshot(baseline_equity=EQUITY, tripped_today=True),
            alert=alert(action=AlertAction.CLOSE, stop_price=None),
            account=account(positions=(position(),)),
        )
    )
    assert decision.is_approved


# --- Rule 9: position sizing --------------------------------------------------


def test_rule9_normal_case_approves_with_quantity() -> None:
    decision = evaluate(snapshot())
    assert decision.is_approved
    assert decision.computed_qty == Decimal("0.08896")


def test_rule9_boundary_qty_exactly_at_min_qty_passes() -> None:
    exact = snapshot(instrument=replace(BTCUSDT, min_qty=Decimal("0.08896")))
    assert evaluate(exact).is_approved


def test_rule9_boundary_qty_just_below_min_qty_rejects() -> None:
    below = snapshot(instrument=replace(BTCUSDT, min_qty=Decimal("0.08897")))
    assert ReasonCode(evaluate(below).reason_code) is ReasonCode.SIZE_BELOW_MINIMUM


def test_rule9_notional_below_exchange_minimum_rejects() -> None:
    below = snapshot(instrument=replace(BTCUSDT, min_notional=Decimal("6000")))
    assert ReasonCode(evaluate(below).reason_code) is ReasonCode.SIZE_BELOW_MINIMUM


def test_rule9_coarse_lot_step_rounding_to_zero_rejects() -> None:
    """A lot step coarser than the computed size rounds down to nothing."""
    coarse = snapshot(instrument=replace(BTCUSDT, lot_step=Decimal("1")))
    assert ReasonCode(evaluate(coarse).reason_code) is ReasonCode.SIZE_BELOW_MINIMUM


# --- Rule 10: exposure caps ---------------------------------------------------


def test_rule10_pyramiding_disabled_rejects_second_entry() -> None:
    held = snapshot(account=account(positions=(position(),)))
    assert ReasonCode(evaluate(held).reason_code) is ReasonCode.EXPOSURE_LIMIT


def test_rule10_pyramiding_enabled_allows_second_entry() -> None:
    held = snapshot(
        account=account(positions=(position(),)),
        config=config(allow_pyramiding=True, max_open_positions=5),
    )
    assert evaluate(held).is_approved


def test_rule10_boundary_at_max_open_positions_rejects() -> None:
    positions = (
        position(symbol="ETHUSDT"),
        position(symbol="SOLUSDT"),
        position(symbol="ADAUSDT"),
    )
    at_cap = snapshot(
        account=account(positions=positions), config=config(max_open_positions=3)
    )
    assert ReasonCode(evaluate(at_cap).reason_code) is ReasonCode.EXPOSURE_LIMIT


def test_rule10_boundary_one_below_max_open_positions_passes() -> None:
    positions = (position(symbol="ETHUSDT"), position(symbol="SOLUSDT"))
    below_cap = snapshot(
        account=account(positions=positions), config=config(max_open_positions=3)
    )
    assert evaluate(below_cap).is_approved


def test_rule10_per_trade_notional_cap_rejects() -> None:
    """Sized notional is ~5515, so a 1000 cap must reject."""
    capped = snapshot(config=config(max_notional_per_trade=Decimal("1000")))
    assert ReasonCode(evaluate(capped).reason_code) is ReasonCode.EXPOSURE_LIMIT


def test_rule10_insufficient_free_balance_rejects() -> None:
    """On spot you cannot buy with money that is already spent."""
    poor = snapshot(account=account(total_equity=EQUITY, free_balance=Decimal("100")))
    assert ReasonCode(evaluate(poor).reason_code) is ReasonCode.EXPOSURE_LIMIT


def test_rule10_total_notional_cap_rejects() -> None:
    tight = snapshot(config=config(max_total_notional=Decimal("1000")))
    assert ReasonCode(evaluate(tight).reason_code) is ReasonCode.EXPOSURE_LIMIT


def test_rule10_exit_bypasses_exposure_caps() -> None:
    """Closing reduces exposure, so caps must never block the way out."""
    decision = evaluate(
        snapshot(
            alert=alert(action=AlertAction.CLOSE, stop_price=None),
            account=account(positions=(position(),)),
            config=config(max_open_positions=1, max_total_notional=Decimal("1")),
        )
    )
    assert decision.is_approved


# --- Engine-level behaviour ---------------------------------------------------


def test_engine_fails_closed_on_internal_error() -> None:
    """Constraint #1 taken literally: a broken engine still answers "no"."""

    class Exploding:
        def as_snapshot_dict(self) -> dict[str, object]:
            return {}

        def __getattr__(self, name: str) -> object:
            raise RuntimeError("boom")

    broken = snapshot()
    object.__setattr__(broken, "config", Exploding())

    decision = evaluate(broken)
    assert not decision.is_approved
    assert ReasonCode(decision.reason_code) is ReasonCode.INTERNAL_ERROR


def test_approved_decision_carries_the_rule_snapshot() -> None:
    """Every decision records the config that produced it (constraint #5)."""
    decision = evaluate(snapshot())
    assert decision.rule_snapshot["risk_per_trade_pct"] == "0.01"
    assert decision.rule_snapshot["version"] == 1


def test_exit_quantity_comes_from_the_held_position() -> None:
    """Sizing an exit could sell more than we own. Use broker truth instead."""
    decision = evaluate(
        snapshot(
            alert=alert(action=AlertAction.CLOSE, stop_price=None),
            account=account(positions=(position(qty="0.137"),)),
        )
    )
    assert decision.computed_qty == Decimal("0.137")


@pytest.mark.parametrize("action", [AlertAction.SELL, AlertAction.CLOSE])
def test_sell_and_close_are_both_exits_on_spot(action: AlertAction) -> None:
    """Spot has no shorting, so `sell` reduces a long rather than opening one."""
    decision = evaluate(
        snapshot(
            alert=alert(action=action, stop_price=None),
            account=account(positions=(position(),)),
        )
    )
    assert decision.is_approved


# --- Exits (plan §4, OQ-6) ----------------------------------------------------


def test_exit_on_a_held_position_is_approved_for_the_held_quantity() -> None:
    """The quantity comes from broker state, never from a calculation.

    Sizing an exit could sell more than we own, or leave a remainder behind and
    call the position closed.
    """
    snap = snapshot(
        alert=alert(action=AlertAction.CLOSE, stop_price=None),
        account=account(positions=(position(qty="0.25"),)),
    )

    decision = evaluate(snap)

    assert decision.verdict is Verdict.APPROVED
    assert decision.computed_qty == Decimal("0.25")


def test_exit_with_no_open_position_is_rejected_not_approved() -> None:
    """OQ-6: a `sell`/`close` with nothing held is refused, not read as a short.

    The distinction that matters is the audit trail. Approving with quantity
    zero would put "APPROVED" in the decision feed for a signal that placed no
    order, telling the user a trade happened when none did.
    """
    snap = snapshot(
        alert=alert(action=AlertAction.SELL, stop_price=None),
        account=account(positions=()),
    )

    decision = evaluate(snap)

    assert decision.verdict is Verdict.REJECTED
    assert decision.reason_code is ReasonCode.NO_POSITION_TO_CLOSE
    assert decision.computed_qty == Decimal("0")


def test_an_exit_needs_no_stop_loss() -> None:
    """You do not attach a protective stop to the order that closes a position."""
    snap = snapshot(
        alert=alert(action=AlertAction.CLOSE, stop_price=None),
        account=account(positions=(position(),)),
    )

    assert evaluate(snap).verdict is Verdict.APPROVED
