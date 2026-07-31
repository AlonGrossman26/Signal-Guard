"""The ten risk rules, in the exact order specified by CLAUDE.md §7.

Each rule is a function taking the snapshot and returning a `Decision` when it
rejects, or `None` to let evaluation continue. The engine runs them in order and
stops at the first rejection.

**Order is part of the specification, not an implementation detail.** A payload
breaking several rules must return the highest-priority reason code, so the
user's dashboard tells them the most important thing that was wrong rather than
whichever check happened to run first. The cheapest and most absolute checks come
first: an account that is locked does not need its payload parsed, and a payload
that will not parse cannot have its symbol checked.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from signalguard.enums import AlertAction, ReasonCode, TradingState, Verdict
from signalguard.risk import breaker
from signalguard.risk.sizing import SizingResult, compute_position_size
from signalguard.risk.types import Decision, Snapshot

Rule = Callable[[Snapshot], Decision | None]


def _reject(
    snapshot: Snapshot, code: ReasonCode, detail: str | None = None
) -> Decision:
    return Decision(
        verdict=Verdict.REJECTED,
        reason_code=code,
        reason_detail=detail,
        rule_snapshot=snapshot.config.as_snapshot_dict(),
    )


# --- 1. Kill switch / trading lock -------------------------------------------


def rule_trading_locked(snapshot: Snapshot) -> Decision | None:
    """Reject when trading is locked.

    Checked at two levels (plan §4, OQ-2). The user-level lock is knowable before
    the payload is parsed, which is what lets rule 1 genuinely precede rule 2;
    the account-level lock can only be checked once the payload has named an
    account, so it is evaluated here too, against the resolved account. Both
    return TRADING_LOCKED, preserving the specified priority.
    """
    if snapshot.user_trading_locked:
        return _reject(snapshot, ReasonCode.TRADING_LOCKED, "user trading lock is on")

    if (
        snapshot.account is not None
        and snapshot.account.trading_state is TradingState.LOCKED
    ):
        return _reject(
            snapshot, ReasonCode.TRADING_LOCKED, "broker account is LOCKED"
        )
    return None


# --- 2. Payload validity ------------------------------------------------------


def rule_payload_valid(snapshot: Snapshot) -> Decision | None:
    """Reject a payload that did not parse, or that is missing what we need.

    Structural parsing happens in the ingress layer (it is HTTP-shaped work);
    this rule consumes the result. Anything unparseable arrives as
    `alert=None` plus a `payload_error`.
    """
    if snapshot.alert is None:
        return _reject(
            snapshot,
            ReasonCode.INVALID_PAYLOAD,
            snapshot.payload_error or "payload could not be parsed",
        )

    alert = snapshot.alert
    if alert.timestamp.tzinfo is None:
        # A naive timestamp is ambiguous, and ambiguity means reject rather than
        # "assume UTC" (constraint #1).
        return _reject(
            snapshot, ReasonCode.INVALID_PAYLOAD, "timestamp has no timezone"
        )

    if alert.order_type.value == "LIMIT" and alert.limit_price is None:
        return _reject(
            snapshot, ReasonCode.INVALID_PAYLOAD, "limit order without limit_price"
        )

    if alert.limit_price is not None and alert.limit_price <= 0:
        return _reject(
            snapshot, ReasonCode.INVALID_PAYLOAD, "limit_price is not positive"
        )
    return None


# --- 3. Staleness -------------------------------------------------------------


def rule_not_stale(snapshot: Snapshot) -> Decision | None:
    """Reject alerts that are too old, or implausibly far in the future.

    Both directions matter. An old alert is a signal for a price that has moved
    on. A future-dated one means a clock is wrong somewhere, and acting on a
    signal whose timing we cannot trust is exactly the ambiguity that constraint
    #1 says to refuse.
    """
    assert snapshot.alert is not None  # rule 2 guarantees this
    config = snapshot.config

    age_sec = (snapshot.now - snapshot.alert.timestamp).total_seconds()

    if age_sec > config.max_alert_age_sec:
        return _reject(
            snapshot,
            ReasonCode.STALE_ALERT,
            f"alert is {age_sec:.1f}s old, limit is {config.max_alert_age_sec}s",
        )

    if age_sec < -config.future_tolerance_sec:
        return _reject(
            snapshot,
            ReasonCode.STALE_ALERT,
            f"alert is {-age_sec:.1f}s in the future, tolerance is "
            f"{config.future_tolerance_sec}s",
        )
    return None


# --- 4. Duplicate -------------------------------------------------------------


def rule_not_duplicate(snapshot: Snapshot) -> Decision | None:
    """Reject a `dedupe_key` already seen inside the window.

    The lookup itself is I/O (Redis), so it happens before the engine runs and
    arrives here as a fact. Keeping the *decision* in the ordered rule chain is
    what makes a duplicate produce a proper audit record with the right reason
    code, rather than being silently dropped at the door.
    """
    if snapshot.is_duplicate:
        assert snapshot.alert is not None
        return _reject(
            snapshot,
            ReasonCode.DUPLICATE_ALERT,
            f"dedupe_key seen within {snapshot.config.dedupe_window_sec}s",
        )
    return None


# --- 5. Symbol allowlist ------------------------------------------------------


def rule_symbol_allowed(snapshot: Snapshot) -> Decision | None:
    """Reject symbols the user has not explicitly opted into.

    An empty allowlist permits nothing. Default deny is the whole point: a new
    or misconfigured profile trades nothing rather than everything.
    """
    assert snapshot.alert is not None
    symbol = snapshot.alert.symbol
    if symbol not in snapshot.config.allowed_symbols:
        return _reject(
            snapshot,
            ReasonCode.SYMBOL_NOT_ALLOWED,
            f"{symbol} is not in the allowed symbol list",
        )
    return None


# --- 6. Mandatory stop-loss ---------------------------------------------------


def rule_stop_loss(snapshot: Snapshot) -> Decision | None:
    """Require a sane protective stop on every entry.

    Skipped for exits: you do not attach a protective stop to the order that
    closes a position, and requiring one would make it impossible to get out
    (plan §4, OQ-6).

    The minimum-distance check is the important half. A stop one tick from entry
    passes a naive "is there a stop?" test while producing an enormous position
    size, since size is inversely proportional to stop distance. That is the
    classic way this feature gets exploited by a buggy script.
    """
    assert snapshot.alert is not None
    alert = snapshot.alert

    if alert.is_exit:
        return None

    entry_price = snapshot.reference_price
    if entry_price is None or entry_price <= 0:
        return _reject(
            snapshot,
            ReasonCode.NO_STOP_LOSS,
            "no reference price available to validate the stop against",
        )

    if alert.stop_price is None or alert.stop_price <= 0:
        return _reject(snapshot, ReasonCode.NO_STOP_LOSS, "stop_price is missing or zero")

    # Spot is long-only (plan §4, OQ-6), so a stop must sit below entry. The
    # short-side branch is implemented for when a futures adapter arrives, and is
    # unreachable through the spot adapter.
    is_long = alert.action is AlertAction.BUY
    if is_long and alert.stop_price >= entry_price:
        return _reject(
            snapshot,
            ReasonCode.NO_STOP_LOSS,
            f"long stop {alert.stop_price} must be below entry {entry_price}",
        )
    if not is_long and alert.stop_price <= entry_price:
        return _reject(
            snapshot,
            ReasonCode.NO_STOP_LOSS,
            f"short stop {alert.stop_price} must be above entry {entry_price}",
        )

    stop_distance = abs(entry_price - alert.stop_price)
    min_distance = entry_price * snapshot.config.min_stop_distance_pct
    if stop_distance < min_distance:
        return _reject(
            snapshot,
            ReasonCode.NO_STOP_LOSS,
            f"stop distance {stop_distance} is below the minimum {min_distance} "
            f"({snapshot.config.min_stop_distance_pct:%} of price)",
        )
    return None


# --- 7. Circuit breaker -------------------------------------------------------


def rule_circuit_breaker(snapshot: Snapshot) -> Decision | None:
    """Reject new entries while the consecutive-loss breaker is open.

    Exits are always allowed through: a breaker exists to stop you opening new
    risk after a losing streak, not to trap you in a position you are trying to
    leave.
    """
    assert snapshot.alert is not None
    if snapshot.alert.is_exit:
        return None

    blocking, reason = breaker.is_blocking(
        snapshot.circuit, snapshot.config, snapshot.now
    )
    if blocking:
        return _reject(snapshot, ReasonCode.CIRCUIT_BREAKER_OPEN, reason)
    return None


# --- 8. Daily drawdown --------------------------------------------------------


def rule_daily_drawdown(snapshot: Snapshot) -> Decision | None:
    """Reject new entries once the daily loss limit is reached.

    Loss is measured mark-to-market, so it includes unrealized PnL. Once tripped,
    the block persists until the next daily reset **even if equity recovers** —
    otherwise a user who breached their limit and bounced could keep trading,
    which defeats the purpose of a daily loss limit.

    Exits are allowed through, for the same reason as the circuit breaker.
    """
    assert snapshot.alert is not None
    if snapshot.alert.is_exit:
        return None

    if snapshot.drawdown.tripped_today:
        return _reject(
            snapshot,
            ReasonCode.DAILY_DRAWDOWN_HIT,
            "daily drawdown limit already hit; blocked until the next reset",
        )

    if snapshot.account is None:
        return _reject(
            snapshot,
            ReasonCode.DAILY_DRAWDOWN_HIT,
            "account state unavailable; cannot verify drawdown",
        )

    baseline = snapshot.drawdown.baseline_equity
    if baseline <= 0:
        # No usable baseline means the limit cannot be evaluated. Fail closed.
        return _reject(
            snapshot,
            ReasonCode.DAILY_DRAWDOWN_HIT,
            "no valid equity baseline for the current session",
        )

    drawdown = (baseline - snapshot.account.total_equity) / baseline
    if drawdown >= snapshot.config.max_daily_dd_pct:
        return _reject(
            snapshot,
            ReasonCode.DAILY_DRAWDOWN_HIT,
            f"drawdown {drawdown:.4%} has reached the limit "
            f"{snapshot.config.max_daily_dd_pct:.4%}",
        )
    return None


# --- 9. Position sizing -------------------------------------------------------


def rule_position_size(snapshot: Snapshot) -> Decision | None:
    """Size the position, rejecting if it lands under an exchange minimum.

    This rule is a transform: on success it does not return a rejection but the
    engine reads the computed quantity back out via `size_for`. Ceiling breaches
    are left to rule 10, because "too large" is an exposure limit, not a
    below-minimum failure (OQ-7).
    """
    assert snapshot.alert is not None
    if snapshot.alert.is_exit:
        return None

    result = size_for(snapshot)
    if result is None:
        return _reject(
            snapshot,
            ReasonCode.SIZE_BELOW_MINIMUM,
            "cannot size: missing instrument, account state, or reference price",
        )
    if result.below_minimum:
        return _reject(snapshot, ReasonCode.SIZE_BELOW_MINIMUM, result.detail)
    return None


def size_for(snapshot: Snapshot) -> SizingResult | None:
    """Run the sizing transform for this snapshot, or None if inputs are missing."""
    alert = snapshot.alert
    if (
        alert is None
        or alert.stop_price is None
        or snapshot.account is None
        or snapshot.instrument is None
        or snapshot.reference_price is None
    ):
        return None
    return compute_position_size(
        entry_price=snapshot.reference_price,
        stop_price=alert.stop_price,
        total_equity=snapshot.account.total_equity,
        free_balance=snapshot.account.free_balance,
        config=snapshot.config,
        instrument=snapshot.instrument,
    )


# --- 10. Exposure caps --------------------------------------------------------


def rule_exposure(snapshot: Snapshot) -> Decision | None:
    """Reject when the new position would breach an exposure limit."""
    assert snapshot.alert is not None
    alert = snapshot.alert
    if alert.is_exit:
        return None

    if snapshot.account is None:
        return _reject(
            snapshot, ReasonCode.EXPOSURE_LIMIT, "account state unavailable"
        )

    account = snapshot.account
    config = snapshot.config

    existing = account.position_for(alert.symbol)
    if existing is not None and existing.qty != 0 and not config.allow_pyramiding:
        return _reject(
            snapshot,
            ReasonCode.EXPOSURE_LIMIT,
            f"already holding {alert.symbol} and pyramiding is disabled",
        )

    if account.open_position_count >= config.max_open_positions:
        return _reject(
            snapshot,
            ReasonCode.EXPOSURE_LIMIT,
            f"{account.open_position_count} open positions, limit is "
            f"{config.max_open_positions}",
        )

    # Per-trade notional and affordability ceilings, computed during sizing.
    result = size_for(snapshot)
    if result is not None and result.exceeds_caps:
        return _reject(snapshot, ReasonCode.EXPOSURE_LIMIT, result.detail)

    new_notional = result.notional if result is not None else Decimal("0")
    if account.total_notional + new_notional > config.max_total_notional:
        return _reject(
            snapshot,
            ReasonCode.EXPOSURE_LIMIT,
            f"total notional {account.total_notional + new_notional} would exceed "
            f"the cap {config.max_total_notional}",
        )
    return None


# The chain, in specification order. The engine walks this list and nothing else,
# so the order here IS the order in CLAUDE.md §7 — keep them identical.
ORDERED_RULES: tuple[Rule, ...] = (
    rule_trading_locked,      # 1
    rule_payload_valid,       # 2
    rule_not_stale,           # 3
    rule_not_duplicate,       # 4
    rule_symbol_allowed,      # 5
    rule_stop_loss,           # 6
    rule_circuit_breaker,     # 7
    rule_daily_drawdown,      # 8
    rule_position_size,       # 9
    rule_exposure,            # 10
)
