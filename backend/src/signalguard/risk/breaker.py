"""Circuit-breaker arithmetic (CLAUDE.md §7 rule 7). Pure — no I/O, no clock.

The breaker counts *closed trades* with realized PnL < 0, consecutively, most
recent first. Any win resets the count to zero.

Two details worth stating, because both are easy to get subtly wrong:

* **Only closed trades count.** An open position that is currently underwater is
  not a loss — it is an opinion. Counting it would trip the breaker on
  volatility rather than on realized failure.
* **A break-even trade is not a win.** `realized_pnl == 0` neither counts as a
  loss nor resets the streak; it is skipped. Treating it as a win would let a
  string of scratch trades silently clear a genuine losing streak.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from signalguard.enums import CircuitState
from signalguard.risk.types import CircuitBreakerSnapshot, RiskConfig


def count_consecutive_losses(realized_pnls_newest_first: Sequence[Decimal]) -> int:
    """Count the current losing streak from a most-recent-first PnL sequence."""
    streak = 0
    for pnl in realized_pnls_newest_first:
        if pnl < 0:
            streak += 1
        elif pnl > 0:
            break  # a win resets the streak
        # pnl == 0: scratch trade, neither counts nor resets
    return streak


def is_blocking(
    breaker: CircuitBreakerSnapshot, config: RiskConfig, now: datetime
) -> tuple[bool, str | None]:
    """Return (blocking, human-readable reason).

    The breaker blocks when it has tripped and has not yet been cleared. It is
    cleared either by the cooldown elapsing or, when the profile demands it, by a
    manual reset — and `circuit_breaker_manual_reset` deliberately outranks the
    cooldown, so a user who asked to be stopped stays stopped until they say
    otherwise.
    """
    tripped = (
        breaker.state is CircuitState.OPEN
        or breaker.consecutive_losses >= config.consecutive_loss_threshold
    )
    if not tripped:
        return False, None

    if config.circuit_breaker_manual_reset:
        return True, (
            f"{breaker.consecutive_losses} consecutive losses; "
            "manual reset required"
        )

    if breaker.cooldown_until is None:
        # Tripped with no cooldown recorded. Unknown expiry is not "expired" —
        # fail closed and keep blocking until something clears it explicitly.
        return True, (
            f"{breaker.consecutive_losses} consecutive losses; no cooldown recorded"
        )

    if now < breaker.cooldown_until:
        return True, (
            f"{breaker.consecutive_losses} consecutive losses; "
            f"cooling down until {breaker.cooldown_until.isoformat()}"
        )

    return False, None
