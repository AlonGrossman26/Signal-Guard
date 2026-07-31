"""Circuit-breaker arithmetic (CLAUDE.md §7 rule 7, §13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from signalguard.enums import CircuitState
from signalguard.risk.breaker import count_consecutive_losses, is_blocking
from signalguard.risk.types import CircuitBreakerSnapshot
from tests.risk.builders import config

NOW = datetime(2026, 7, 31, 10, 15, tzinfo=UTC)


def pnl(*values: str) -> list[Decimal]:
    """Realized PnL, most recent first."""
    return [Decimal(v) for v in values]


# --- Counting the streak ------------------------------------------------------


def test_no_trades_means_no_losses() -> None:
    assert count_consecutive_losses([]) == 0


def test_counts_consecutive_losses_from_most_recent() -> None:
    assert count_consecutive_losses(pnl("-10", "-20", "-5")) == 3


def test_a_win_resets_the_streak() -> None:
    """Three losses, but a win happened more recently, so the streak is zero."""
    assert count_consecutive_losses(pnl("5", "-10", "-20", "-5")) == 0


def test_a_win_stops_the_count_partway() -> None:
    assert count_consecutive_losses(pnl("-10", "-20", "50", "-5", "-5")) == 2


def test_break_even_neither_counts_nor_resets() -> None:
    """A scratch trade is not a win. Treating it as one would clear a real streak."""
    assert count_consecutive_losses(pnl("-10", "0", "-20")) == 2


def test_break_even_alone_is_not_a_loss() -> None:
    assert count_consecutive_losses(pnl("0", "0", "0")) == 0


# --- Blocking behaviour -------------------------------------------------------


def test_closed_breaker_does_not_block() -> None:
    blocking, _ = is_blocking(CircuitBreakerSnapshot(), config(), NOW)
    assert not blocking


def test_below_threshold_does_not_block() -> None:
    breaker = CircuitBreakerSnapshot(consecutive_losses=2)
    blocking, _ = is_blocking(breaker, config(consecutive_loss_threshold=3), NOW)
    assert not blocking


def test_boundary_exactly_at_threshold_blocks() -> None:
    breaker = CircuitBreakerSnapshot(
        consecutive_losses=3, cooldown_until=NOW + timedelta(minutes=1)
    )
    blocking, _ = is_blocking(breaker, config(consecutive_loss_threshold=3), NOW)
    assert blocking


def test_within_cooldown_blocks() -> None:
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW + timedelta(minutes=30),
    )
    blocking, reason = is_blocking(breaker, config(), NOW)
    assert blocking
    assert "cooling down" in (reason or "")


def test_boundary_exactly_at_cooldown_expiry_unblocks() -> None:
    """`now < cooldown_until` blocks, so equality means the cooldown has elapsed."""
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN, consecutive_losses=3, cooldown_until=NOW
    )
    blocking, _ = is_blocking(breaker, config(), NOW)
    assert not blocking


def test_one_microsecond_before_expiry_still_blocks() -> None:
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW + timedelta(microseconds=1),
    )
    blocking, _ = is_blocking(breaker, config(), NOW)
    assert blocking


def test_manual_reset_ignores_an_elapsed_cooldown() -> None:
    """A user who asked to be stopped stays stopped until they say otherwise."""
    breaker = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW - timedelta(days=7),
    )
    blocking, reason = is_blocking(
        breaker, config(circuit_breaker_manual_reset=True), NOW
    )
    assert blocking
    assert "manual reset" in (reason or "")


def test_open_with_no_cooldown_recorded_blocks() -> None:
    """Unknown expiry is not "expired" — fail closed."""
    breaker = CircuitBreakerSnapshot(state=CircuitState.OPEN, consecutive_losses=3)
    blocking, _ = is_blocking(breaker, config(), NOW)
    assert blocking


# --- Restart survival ---------------------------------------------------------


def test_blocking_is_a_pure_function_of_stored_state() -> None:
    """The breaker survives a restart because it holds nothing in memory.

    State is read from Postgres/Redis into the snapshot, and `is_blocking` is a
    function of that snapshot plus the current time. A restarted process passes
    the same stored values and gets the same answer — there is no in-process
    counter to lose.
    """
    stored = CircuitBreakerSnapshot(
        state=CircuitState.OPEN,
        consecutive_losses=3,
        cooldown_until=NOW + timedelta(minutes=45),
    )
    before_restart, _ = is_blocking(stored, config(), NOW)
    after_restart, _ = is_blocking(stored, config(), NOW + timedelta(seconds=30))
    assert before_restart is True
    assert after_restart is True

    # And once the stored cooldown genuinely passes, it clears — again with no
    # reference to how long the process has been alive.
    later, _ = is_blocking(stored, config(), NOW + timedelta(hours=1))
    assert later is False


@pytest.mark.parametrize("losses", [0, 1, 2])
def test_streak_below_threshold_never_blocks_regardless_of_time(losses: int) -> None:
    breaker = CircuitBreakerSnapshot(consecutive_losses=losses)
    for offset_hours in (0, 1, 24):
        blocking, _ = is_blocking(
            breaker, config(), NOW + timedelta(hours=offset_hours)
        )
        assert not blocking
