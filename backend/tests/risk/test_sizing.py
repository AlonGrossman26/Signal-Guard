"""Position sizing (CLAUDE.md §7 rule 9, §13 sizing cases)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from signalguard.risk.sizing import compute_position_size, round_down_to_step
from tests.risk.builders import BTCUSDT, ENTRY, EQUITY, STOP, config


def size(**overrides: object):  # type: ignore[no-untyped-def]
    base: dict[str, object] = {
        "entry_price": ENTRY,
        "stop_price": STOP,
        "total_equity": EQUITY,
        "free_balance": EQUITY,
        "config": config(),
        "instrument": BTCUSDT,
    }
    base.update(overrides)
    return compute_position_size(**base)  # type: ignore[arg-type]


# --- round_down_to_step -------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "step", "expected"),
    [
        ("0.0889679", "0.00001", "0.08896"),
        ("1.999999", "0.001", "1.999"),
        ("5", "1", "5"),
        ("4.9999", "1", "4"),
        ("0.5", "1", "0"),
        ("123.456", "0.01", "123.45"),
    ],
)
def test_round_down_never_rounds_up(value: str, step: str, expected: str) -> None:
    """Always down. Rounding up would risk more than the user authorised."""
    assert round_down_to_step(Decimal(value), Decimal(step)) == Decimal(expected)


def test_round_down_rejects_non_positive_step() -> None:
    with pytest.raises(ValueError, match="positive"):
        round_down_to_step(Decimal("1"), Decimal("0"))


# --- The normal case ----------------------------------------------------------


def test_normal_case_matches_the_worked_example() -> None:
    """$10k equity, 1% risk, $62,000 entry, $61,000 stop, 20 bps buffer."""
    result = size()
    assert result.is_tradeable
    assert result.stop_distance == Decimal("1000")
    assert result.risk_amount == Decimal("100.00")
    assert result.qty == Decimal("0.08896")


def test_buffer_is_applied_to_notional_not_to_risk_amount() -> None:
    """OQ-3: the closed form, verified against the literal reading.

    The literal formula (risk_amount x (1 - buffer)) would size 0.0998 BTC and
    lose $112.18 at the stop against a $100 budget — 12% over. This one is exact.
    """
    result = size()
    stop_loss = result.qty * result.stop_distance
    costs = result.qty * ENTRY * (Decimal("20") / Decimal("10000"))
    total_loss = stop_loss + costs

    assert total_loss <= result.risk_amount
    # And it is tight, not merely safe — sized so the budget is nearly exhausted.
    assert total_loss > result.risk_amount * Decimal("0.999")


def test_zero_buffer_reduces_to_the_plain_formula() -> None:
    result = size(config=config(fee_slippage_buffer_bps=0))
    # 100 / 1000 = 0.1 exactly.
    assert result.qty == Decimal("0.1")


def test_larger_buffer_produces_a_smaller_position() -> None:
    small = size(config=config(fee_slippage_buffer_bps=100)).qty
    large = size(config=config(fee_slippage_buffer_bps=0)).qty
    assert small < large


# --- Degenerate stop distances ------------------------------------------------


def test_zero_stop_distance_is_rejected_not_divided_by() -> None:
    """stop == entry is an undefined position, not an infinite one."""
    result = size(stop_price=ENTRY)
    assert result.below_minimum
    assert result.qty == Decimal("0")
    assert "zero" in (result.detail or "")


def test_near_zero_stop_distance_produces_a_huge_size() -> None:
    """Documents *why* rule 6 enforces a minimum stop distance.

    Sizing alone would happily return an enormous position here. The stop-distance
    floor in rule 6 is what stops this input ever reaching the sizing transform.
    """
    result = size(stop_price=Decimal("61999.99"))
    assert result.qty > Decimal("0.5")


def test_non_positive_entry_price_rejected() -> None:
    result = size(entry_price=Decimal("0"))
    assert result.below_minimum


# --- Minimums and ceilings ----------------------------------------------------


def test_rounding_below_min_qty_is_flagged_below_minimum() -> None:
    result = size(instrument=replace(BTCUSDT, min_qty=Decimal("1")))
    assert result.below_minimum
    assert not result.exceeds_caps


def test_notional_below_exchange_minimum_is_flagged() -> None:
    result = size(instrument=replace(BTCUSDT, min_notional=Decimal("10000")))
    assert result.below_minimum


def test_coarse_lot_step_rounding_to_zero_is_flagged() -> None:
    result = size(instrument=replace(BTCUSDT, lot_step=Decimal("1")))
    assert result.below_minimum
    assert result.qty == Decimal("0")


def test_insufficient_balance_is_a_ceiling_not_a_minimum() -> None:
    """Different failures map to different reason codes (OQ-7)."""
    result = size(free_balance=Decimal("100"))
    assert result.exceeds_caps
    assert not result.below_minimum


def test_per_trade_notional_cap_is_a_ceiling() -> None:
    result = size(config=config(max_notional_per_trade=Decimal("1000")))
    assert result.exceeds_caps


def test_size_is_never_capped_silently() -> None:
    """Trading a smaller size than the signal implies would be a silent surprise."""
    result = size(free_balance=Decimal("100"))
    assert result.qty > Decimal("0")  # the computed size is reported as-is
    assert result.exceeds_caps       # and rejected, not quietly shrunk


# --- Equity scaling -----------------------------------------------------------


def test_position_scales_with_equity() -> None:
    """Double the equity, double the size — to within one lot step.

    Not *exactly* double: each result is floored to the lot step independently,
    and `floor(2x) - 2*floor(x)` can be a full step. Asserting exact linearity
    would be asserting that rounding does not happen. The deviation is always
    upward-bounded and never in the risky direction — doubling equity can never
    more than double the size.
    """
    small = size(total_equity=Decimal("10000"), free_balance=Decimal("100000")).qty
    large = size(total_equity=Decimal("20000"), free_balance=Decimal("100000")).qty
    assert large >= small * 2
    assert large - small * 2 <= BTCUSDT.lot_step


def test_wider_stop_produces_a_smaller_position() -> None:
    """Risk per trade is fixed, so a wider stop must mean fewer units."""
    tight = size(stop_price=Decimal("61500")).qty
    wide = size(stop_price=Decimal("60000")).qty
    assert wide < tight
