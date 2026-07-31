"""Property-based test (CLAUDE.md §13, Hypothesis).

    "For any valid inputs, realized loss at the stop never exceeds
     risk_per_trade_pct of equity."

This is the single most important assertion in the project. Every other test
checks a case someone thought of; this one searches for the case nobody did.

It is also the test that decides OQ-3. The literal reading of the fee/slippage
buffer — subtract a percentage of `risk_amount` — fails this property, because
fees and slippage scale with notional rather than with risk. The closed form in
`sizing.py` satisfies it by construction, and `test_literal_formula_would_fail`
at the bottom demonstrates the difference concretely rather than asserting it.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from signalguard.risk.sizing import compute_position_size
from signalguard.risk.types import InstrumentSpec, RiskConfig

# Ranges chosen to span realistic crypto trading: sub-dollar altcoins through
# five-figure BTC, retail accounts through institutional, and every lot step
# Binance actually uses.
equities = st.decimals(min_value=100, max_value=10_000_000, places=2)
prices = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("200000"), places=2)
risk_pcts = st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.5"), places=4)
buffer_bps = st.integers(min_value=0, max_value=500)
lot_steps = st.sampled_from(
    [Decimal("1"), Decimal("0.1"), Decimal("0.001"), Decimal("0.00001"),
     Decimal("0.000001"), Decimal("0.00000001")]
)
# Stop distance as a fraction of entry, from a 0.1% scalp to a 90% disaster stop.
stop_fractions = st.decimals(
    min_value=Decimal("0.001"), max_value=Decimal("0.9"), places=4
)


@settings(max_examples=500, deadline=None)
@given(
    equity=equities,
    entry=prices,
    stop_fraction=stop_fractions,
    risk_pct=risk_pcts,
    bps=buffer_bps,
    lot_step=lot_steps,
)
def test_loss_at_stop_never_exceeds_risk_budget(
    equity: Decimal,
    entry: Decimal,
    stop_fraction: Decimal,
    risk_pct: Decimal,
    bps: int,
    lot_step: Decimal,
) -> None:
    """The core guarantee: a stop-out costs no more than the stated percentage.

    Worst-case loss is the stop distance plus the fee and slippage headroom, both
    charged on the position actually taken.
    """
    stop = entry * (Decimal("1") - stop_fraction)

    config = RiskConfig(
        version=1,
        risk_per_trade_pct=risk_pct,
        fee_slippage_buffer_bps=bps,
        allowed_symbols=frozenset({"TEST"}),
        max_notional_per_trade=Decimal("1e30"),
        max_total_notional=Decimal("1e30"),
    )
    instrument = InstrumentSpec(
        symbol="TEST",
        tick_size=Decimal("0.01"),
        lot_step=lot_step,
        min_qty=Decimal("0"),
        min_notional=Decimal("0"),
    )

    result = compute_position_size(
        entry_price=entry,
        stop_price=stop,
        total_equity=equity,
        free_balance=Decimal("1e30"),
        config=config,
        instrument=instrument,
    )

    risk_budget = equity * risk_pct
    buffer_rate = Decimal(bps) / Decimal("10000")
    worst_case_loss = (
        result.qty * result.stop_distance + result.qty * entry * buffer_rate
    )

    assert worst_case_loss <= risk_budget, (
        f"loss {worst_case_loss} exceeded budget {risk_budget} "
        f"(qty={result.qty}, entry={entry}, stop={stop}, bps={bps})"
    )


@settings(max_examples=200, deadline=None)
@given(
    equity=equities,
    entry=prices,
    stop_fraction=stop_fractions,
    risk_pct=risk_pcts,
    lot_step=lot_steps,
)
def test_quantity_is_always_a_whole_multiple_of_the_lot_step(
    equity: Decimal,
    entry: Decimal,
    stop_fraction: Decimal,
    risk_pct: Decimal,
    lot_step: Decimal,
) -> None:
    """An exchange rejects a quantity that is not a multiple of its lot step."""
    stop = entry * (Decimal("1") - stop_fraction)
    config = RiskConfig(
        version=1,
        risk_per_trade_pct=risk_pct,
        allowed_symbols=frozenset({"TEST"}),
        max_notional_per_trade=Decimal("1e30"),
        max_total_notional=Decimal("1e30"),
    )
    instrument = InstrumentSpec(
        symbol="TEST",
        tick_size=Decimal("0.01"),
        lot_step=lot_step,
        min_qty=Decimal("0"),
        min_notional=Decimal("0"),
    )

    result = compute_position_size(
        entry_price=entry,
        stop_price=stop,
        total_equity=equity,
        free_balance=Decimal("1e30"),
        config=config,
        instrument=instrument,
    )
    assert result.qty % lot_step == 0


@settings(max_examples=200, deadline=None)
@given(equity=equities, entry=prices, stop_fraction=stop_fractions)
def test_quantity_is_never_negative(
    equity: Decimal, entry: Decimal, stop_fraction: Decimal
) -> None:
    """A negative quantity would be a sell order dressed as a buy."""
    stop = entry * (Decimal("1") - stop_fraction)
    config = RiskConfig(version=1, allowed_symbols=frozenset({"TEST"}))
    instrument = InstrumentSpec(
        symbol="TEST",
        tick_size=Decimal("0.01"),
        lot_step=Decimal("0.00001"),
        min_qty=Decimal("0"),
        min_notional=Decimal("0"),
    )
    result = compute_position_size(
        entry_price=entry,
        stop_price=stop,
        total_equity=equity,
        free_balance=Decimal("1e30"),
        config=config,
        instrument=instrument,
    )
    assert result.qty >= 0


def test_literal_formula_would_fail_this_property() -> None:
    """Demonstrates OQ-3 concretely, using CLAUDE.md's own worked numbers.

    Not an assertion about our code — a demonstration of why our code differs
    from the literal wording of the spec. If this ever stops failing, the
    reasoning in the plan needs revisiting.
    """
    equity = Decimal("10000")
    entry = Decimal("62000")
    stop = Decimal("61000")
    risk_pct = Decimal("0.01")
    buffer_rate = Decimal("0.002")  # 20 bps

    risk_budget = equity * risk_pct              # 100
    stop_distance = entry - stop                 # 1000

    # The literal reading: subtract the buffer from risk_amount, then divide.
    literal_qty = (risk_budget * (Decimal("1") - buffer_rate)) / stop_distance
    literal_loss = literal_qty * stop_distance + literal_qty * entry * buffer_rate

    assert literal_loss > risk_budget
    # ~$112.18 against a $100 budget: 12% over.
    assert literal_loss > risk_budget * Decimal("1.12")

    # The closed form lands exactly on budget.
    closed_form_qty = risk_budget / (stop_distance + entry * buffer_rate)
    closed_form_loss = (
        closed_form_qty * stop_distance + closed_form_qty * entry * buffer_rate
    )
    assert abs(closed_form_loss - risk_budget) < Decimal("0.0000001")
