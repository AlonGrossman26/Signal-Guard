"""Input and output types for the risk engine.

Everything here is a frozen dataclass. Immutability is not stylistic: a rule that
could mutate the snapshot would make rule order matter in ways the spec does not
describe, and a bug in rule 3 could silently change what rule 9 sees.

**No I/O appears in this package.** The engine is handed a fully-populated
`Snapshot` — including `now`, the current time — and returns a `Decision`. That
is what lets the whole rule set be tested exhaustively in milliseconds, and it is
the single most important design decision in the project.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from signalguard.enums import (
    AlertAction,
    CircuitState,
    OrderType,
    ReasonCode,
    TradingState,
    Verdict,
)


@dataclass(frozen=True)
class RiskConfig:
    """The user's risk rules, as they stood at evaluation time.

    A copy of this is written into every decision's `rule_snapshot`, so a later
    profile edit can never rewrite what the rules were when a decision was made.
    """

    version: int

    # Rule 3 — staleness
    max_alert_age_sec: int = 30
    future_tolerance_sec: int = 5

    # Rule 4 — duplicates
    dedupe_window_sec: int = 60

    # Rule 5 — symbol allowlist. Empty means nothing is tradeable: default deny.
    allowed_symbols: frozenset[str] = frozenset()

    # Rule 6 — mandatory stop-loss
    min_stop_distance_pct: Decimal = Decimal("0.001")

    # Rule 7 — circuit breaker
    consecutive_loss_threshold: int = 3
    circuit_breaker_cooldown_minutes: int = 60
    circuit_breaker_manual_reset: bool = False

    # Rule 8 — daily drawdown
    max_daily_dd_pct: Decimal = Decimal("0.05")

    # Rule 9 — position sizing
    risk_per_trade_pct: Decimal = Decimal("0.01")
    fee_slippage_buffer_bps: int = 20

    # Rule 10 — exposure caps
    max_notional_per_trade: Decimal = Decimal("1000")
    max_open_positions: int = 3
    max_total_notional: Decimal = Decimal("5000")
    allow_pyramiding: bool = False

    def as_snapshot_dict(self) -> dict[str, object]:
        """JSON-safe copy for `decisions.rule_snapshot`.

        Decimals become strings rather than floats — a rule snapshot that
        recorded 0.1 as 0.1000000000000000055 would be a misleading audit record.
        """
        return {
            "version": self.version,
            "max_alert_age_sec": self.max_alert_age_sec,
            "future_tolerance_sec": self.future_tolerance_sec,
            "dedupe_window_sec": self.dedupe_window_sec,
            "allowed_symbols": sorted(self.allowed_symbols),
            "min_stop_distance_pct": str(self.min_stop_distance_pct),
            "consecutive_loss_threshold": self.consecutive_loss_threshold,
            "circuit_breaker_cooldown_minutes": self.circuit_breaker_cooldown_minutes,
            "circuit_breaker_manual_reset": self.circuit_breaker_manual_reset,
            "max_daily_dd_pct": str(self.max_daily_dd_pct),
            "risk_per_trade_pct": str(self.risk_per_trade_pct),
            "fee_slippage_buffer_bps": self.fee_slippage_buffer_bps,
            "max_notional_per_trade": str(self.max_notional_per_trade),
            "max_open_positions": self.max_open_positions,
            "max_total_notional": str(self.max_total_notional),
            "allow_pyramiding": self.allow_pyramiding,
        }


@dataclass(frozen=True)
class InstrumentSpec:
    """Exchange trading filters. Always fetched, never hardcoded (CLAUDE.md §7)."""

    symbol: str
    tick_size: Decimal
    lot_step: Decimal
    min_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class OpenPosition:
    symbol: str
    qty: Decimal
    avg_entry: Decimal
    mark_price: Decimal

    @property
    def notional(self) -> Decimal:
        """Current mark-to-market value of the holding."""
        return abs(self.qty) * self.mark_price


@dataclass(frozen=True)
class AccountState:
    """Broker ground truth at evaluation time.

    Three separate money fields rather than one ambiguous `liquid_equity`
    (plan §3, Q3):

    * `total_equity` — mark-to-market (cash + holdings). The base for sizing and
      the basis for drawdown, because "1% of my account" should mean the same
      thing whether or not a position happens to be open.
    * `free_balance` — spendable cash. The hard affordability ceiling: on spot
      you cannot buy with money already spent.
    * `position_value` — the holdings half of `total_equity`, kept for display.
    """

    trading_state: TradingState
    total_equity: Decimal
    free_balance: Decimal
    position_value: Decimal
    positions: tuple[OpenPosition, ...] = ()

    def position_for(self, symbol: str) -> OpenPosition | None:
        for position in self.positions:
            if position.symbol == symbol:
                return position
        return None

    @property
    def open_position_count(self) -> int:
        """Positions with a non-zero quantity. A dust balance is not a position."""
        return sum(1 for p in self.positions if p.qty != 0)

    @property
    def total_notional(self) -> Decimal:
        return sum((p.notional for p in self.positions), start=Decimal("0"))


@dataclass(frozen=True)
class CircuitBreakerSnapshot:
    """Circuit-breaker state, read from Postgres/Redis before the engine runs."""

    state: CircuitState = CircuitState.CLOSED
    consecutive_losses: int = 0
    # None while OPEN means manual reset only — time will not clear it.
    cooldown_until: datetime | None = None


@dataclass(frozen=True)
class DrawdownSnapshot:
    """Daily drawdown inputs.

    `tripped_today` exists because CLAUDE.md §7 requires the block to persist
    until the next reset **even if equity recovers**. Without it, a user who
    breached the limit and then bounced back would be allowed to keep trading —
    exactly the behaviour a daily loss limit is meant to prevent.
    """

    baseline_equity: Decimal
    tripped_today: bool = False


@dataclass(frozen=True)
class AlertInput:
    """A structurally-valid alert.

    If parsing failed, the snapshot carries `alert=None` and a `payload_error`
    instead, and rule 2 rejects — which keeps parsing (I/O-adjacent, HTTP-shaped)
    out of this package while still letting rule 2 fire in its specified order.
    """

    symbol: str
    action: AlertAction
    order_type: OrderType
    timestamp: datetime
    dedupe_key: str
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    take_profit: Decimal | None = None

    @property
    def is_exit(self) -> bool:
        """True for actions that reduce exposure rather than open it.

        On Binance spot there is no shorting (plan §4, OQ-6): `buy` opens or
        increases a long, while `sell` and `close` both exit one. Exits are not
        subject to the stop-loss rule or to sizing — you do not attach a
        protective stop to the order that closes the position, and the quantity
        is whatever is currently held rather than something to compute.
        """
        return self.action in (AlertAction.SELL, AlertAction.CLOSE)


@dataclass(frozen=True)
class Snapshot:
    """Everything the engine needs. Assembled by the caller; never fetched here.

    `now` is passed in rather than read. A rule that called `datetime.now()`
    would be untestable at a boundary — and every interesting risk bug lives
    exactly at a boundary.
    """

    now: datetime
    config: RiskConfig
    circuit: CircuitBreakerSnapshot
    drawdown: DrawdownSnapshot

    # Rule 1 — a user-level lock, checkable before the payload is parsed. The
    # account-level lock lives on `account.trading_state` and is checked once the
    # payload has named an account (plan §4, OQ-2).
    user_trading_locked: bool = False

    # Rule 2 — exactly one of these is set.
    alert: AlertInput | None = None
    payload_error: str | None = None

    # Rule 4 — computed by the ingress layer against Redis, passed in as a fact.
    is_duplicate: bool = False

    account: AccountState | None = None
    instrument: InstrumentSpec | None = None

    # Sizing needs a price. For a limit order that is `limit_price`; for a market
    # order there is no price in the payload, so the caller supplies the current
    # reference price fetched at evaluation time (plan §2, A8).
    reference_price: Decimal | None = None


@dataclass(frozen=True)
class Decision:
    """The engine's output. Always written to `decisions` before any order."""

    verdict: Verdict
    reason_code: ReasonCode
    rule_snapshot: dict[str, object]
    reason_detail: str | None = None
    computed_qty: Decimal | None = None
    entry_reference_price: Decimal | None = None
    stop_price: Decimal | None = None

    @property
    def is_approved(self) -> bool:
        return self.verdict is Verdict.APPROVED
