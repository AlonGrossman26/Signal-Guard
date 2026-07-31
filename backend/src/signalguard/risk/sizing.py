"""Position sizing — a transform, not merely a check (CLAUDE.md §7 rule 9).

The specified formula is:

    risk_amount   = liquid_equity x risk_per_trade_pct
    stop_distance = abs(entry_price - stop_price)
    raw_qty       = risk_amount / stop_distance
    qty           = round_down_to_step(raw_qty, lot_step)

with a fee + slippage buffer applied so that a worst-case stop-out still loses no
more than the stated risk percentage.

**How the buffer is applied (plan §4, OQ-3).** CLAUDE.md says to subtract the
buffer from `risk_amount` first. That under-corrects, because fees and slippage
scale with *notional* while `risk_amount` is a slice of *equity* — the two are
related by the stop distance, which varies per trade. Worked example: $10,000
equity, 1% risk, $62,000 entry, $61,000 stop, 20 bps buffer.

    literal:      qty = (100 x 0.998) / 1000               = 0.0998
                  worst case = 99.80 + 0.0998x62000x0.002  = $112.18   over by 12%

    closed form:  qty = 100 / (1000 + 62000 x 0.002)       = 0.0889...
                  worst case = 88.97 + 11.03               = $100.00   exact

Total loss at the stop is `qty x stop_distance + qty x entry_price x buffer_rate`.
Setting that equal to `risk_amount` and solving for qty needs no iteration:

    qty = risk_amount / (stop_distance + entry_price x buffer_rate)

The Hypothesis property test in CLAUDE.md §13 asserts realized loss at the stop
never exceeds `risk_per_trade_pct` of equity. The literal formula fails that
test; this one satisfies it by construction, and rounding down only adds margin.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, localcontext

from signalguard.risk.types import InstrumentSpec, RiskConfig

# Basis points -> fraction. 20 bps = 0.0020.
_BPS = Decimal("10000")

# Sizing divides quantities that can span from 1e-18 lot steps to large
# notionals. The default 28-significant-digit context can go inexact across that
# range, so the arithmetic runs at higher precision. This is about the division
# staying exact, not about storing more digits than the exchange accepts.
_SIZING_PRECISION = 60


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    """Round `value` down to a whole multiple of `step`.

    Always **down**, never nearest: rounding a quantity up would risk slightly
    more than the user authorised, and "slightly more than authorised" repeated
    across every trade is how a risk limit stops meaning anything.
    """
    if step <= 0:
        raise ValueError("lot step must be positive")
    with localcontext() as ctx:
        ctx.prec = _SIZING_PRECISION
        steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return steps * step


@dataclass(frozen=True)
class SizingResult:
    """Outcome of the sizing transform.

    `below_minimum` and `exceeds_caps` are reported separately because they map
    to different reason codes: falling under an exchange minimum is
    SIZE_BELOW_MINIMUM (rule 9), while breaching a notional or balance ceiling is
    an exposure limit (rule 10). See OQ-7.
    """

    qty: Decimal
    risk_amount: Decimal
    stop_distance: Decimal
    notional: Decimal
    below_minimum: bool = False
    exceeds_caps: bool = False
    detail: str | None = None

    @property
    def is_tradeable(self) -> bool:
        return not self.below_minimum and not self.exceeds_caps and self.qty > 0


def compute_position_size(
    *,
    entry_price: Decimal,
    stop_price: Decimal,
    total_equity: Decimal,
    free_balance: Decimal,
    config: RiskConfig,
    instrument: InstrumentSpec,
) -> SizingResult:
    """Size a position so a stop-out costs no more than `risk_per_trade_pct`.

    `total_equity` is mark-to-market and sets the risk budget; `free_balance` is
    spendable cash and caps what can actually be bought (plan §3, Q3).
    """
    zero = Decimal("0")
    stop_distance = abs(entry_price - stop_price)

    # Guard before dividing. A zero stop distance is not a large position — it is
    # an undefined one, and the classic way this feature gets exploited by a
    # buggy script sending stop == entry.
    if stop_distance <= 0:
        return SizingResult(
            qty=zero,
            risk_amount=zero,
            stop_distance=zero,
            notional=zero,
            below_minimum=True,
            detail="stop distance is zero",
        )
    if entry_price <= 0:
        return SizingResult(
            qty=zero,
            risk_amount=zero,
            stop_distance=stop_distance,
            notional=zero,
            below_minimum=True,
            detail="entry price is not positive",
        )

    with localcontext() as ctx:
        ctx.prec = _SIZING_PRECISION

        risk_amount = total_equity * config.risk_per_trade_pct
        buffer_rate = Decimal(config.fee_slippage_buffer_bps) / _BPS

        # The closed form. `cost_per_unit` is what one unit costs in the worst
        # case: the stop loss itself, plus the fee and slippage headroom charged
        # on that unit's notional.
        cost_per_unit = stop_distance + (entry_price * buffer_rate)
        raw_qty = risk_amount / cost_per_unit

        qty = round_down_to_step(raw_qty, instrument.lot_step)
        notional = qty * entry_price

    if qty <= 0:
        return SizingResult(
            qty=zero,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            notional=zero,
            below_minimum=True,
            detail="computed quantity rounds down to zero at this lot step",
        )

    # --- Exchange minimums -> SIZE_BELOW_MINIMUM (rule 9) --------------------
    if qty < instrument.min_qty:
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            notional=notional,
            below_minimum=True,
            detail=f"qty {qty} below exchange minimum {instrument.min_qty}",
        )
    if notional < instrument.min_notional:
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            notional=notional,
            below_minimum=True,
            detail=(
                f"notional {notional} below exchange minimum "
                f"{instrument.min_notional}"
            ),
        )

    # --- Ceilings -> EXPOSURE_LIMIT (rule 10) --------------------------------
    # Deliberately NOT capped down to fit. Silently trading a smaller size than
    # the signal implies is a surprise, and surprises in an execution path are
    # how trust is lost. Rejecting is visible and recoverable. (See OQ-7.)
    if notional > config.max_notional_per_trade:
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            notional=notional,
            exceeds_caps=True,
            detail=(
                f"notional {notional} exceeds max_notional_per_trade "
                f"{config.max_notional_per_trade}"
            ),
        )
    if notional > free_balance:
        return SizingResult(
            qty=qty,
            risk_amount=risk_amount,
            stop_distance=stop_distance,
            notional=notional,
            exceeds_caps=True,
            detail=f"notional {notional} exceeds free balance {free_balance}",
        )

    return SizingResult(
        qty=qty,
        risk_amount=risk_amount,
        stop_distance=stop_distance,
        notional=notional,
    )
