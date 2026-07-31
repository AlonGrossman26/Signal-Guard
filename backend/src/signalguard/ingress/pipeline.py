"""Orchestration around the pure risk engine.

This module does the I/O the engine refuses to: it gathers broker state, reads
the clock once, loads the profile, assembles a `Snapshot`, calls `evaluate`, and
persists the result.

**Every exit path writes a decision row.** Including the paths where the engine
never runs — the broker is unreachable, Redis is down, the account does not
exist. Constraint #5 says every alert gets an auditable outcome, and "we could
not evaluate this" is an outcome a user needs to see. Those cases use the
pipeline reason codes (plan §4, OQ-1), kept distinct from rule codes so the
dashboard's rejection breakdown does not lie about *why* trades were refused.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import (
    Alert,
    BrokerAccount,
    CircuitBreakerState,
    EquitySnapshot,
    Instrument,
    RiskProfile,
)
from signalguard.db.models import (
    Decision as DecisionRow,
)
from signalguard.enums import (
    AlertAction,
    CircuitState,
    OrderType,
    ReasonCode,
    TradingState,
    Verdict,
)
from signalguard.risk import evaluate
from signalguard.risk.session import session_date_for
from signalguard.risk.types import (
    AccountState,
    AlertInput,
    CircuitBreakerSnapshot,
    Decision,
    DrawdownSnapshot,
    InstrumentSpec,
    OpenPosition,
    RiskConfig,
    Snapshot,
)

logger = logging.getLogger(__name__)

# Instrument filters older than this are not trusted (plan §4, OQ-5). Sizing
# against stale exchange minimums risks orders the exchange will reject, or worse,
# accept at the wrong size.
MAX_INSTRUMENT_AGE_SEC = 24 * 3600

# Injected so the ingress layer never imports the execution layer (CLAUDE.md §5)
# and so tests can supply a fake broker. Returns (equity, free_balance, positions);
# raising means the broker is unreachable, which is a rejection (plan §3, Q2).
AccountStateProvider = Callable[
    [BrokerAccount], Awaitable[tuple[Decimal, Decimal, tuple[OpenPosition, ...]]]
]


@dataclass(frozen=True)
class PipelineResult:
    decision: Decision
    decision_id: uuid.UUID
    latency_ms: int


def _pipeline_rejection(
    config: RiskConfig | None, code: ReasonCode, detail: str
) -> Decision:
    """A rejection from outside the engine — no snapshot could be assembled."""
    return Decision(
        verdict=Verdict.REJECTED,
        reason_code=code,
        reason_detail=detail,
        rule_snapshot=config.as_snapshot_dict() if config else {},
    )


def profile_to_config(profile: RiskProfile) -> RiskConfig:
    """Map the stored profile onto the engine's frozen config."""
    return RiskConfig(
        version=profile.version,
        max_alert_age_sec=profile.max_alert_age_sec,
        future_tolerance_sec=profile.future_tolerance_sec,
        dedupe_window_sec=profile.dedupe_window_sec,
        allowed_symbols=frozenset(profile.allowed_symbols or ()),
        min_stop_distance_pct=profile.min_stop_distance_pct,
        consecutive_loss_threshold=profile.consecutive_loss_threshold,
        circuit_breaker_cooldown_minutes=profile.circuit_breaker_cooldown_minutes,
        circuit_breaker_manual_reset=profile.circuit_breaker_manual_reset,
        max_daily_dd_pct=profile.max_daily_dd_pct,
        risk_per_trade_pct=profile.risk_per_trade_pct,
        fee_slippage_buffer_bps=profile.fee_slippage_buffer_bps,
        max_notional_per_trade=profile.max_notional_per_trade,
        max_open_positions=profile.max_open_positions,
        max_total_notional=profile.max_total_notional,
        allow_pyramiding=profile.allow_pyramiding,
    )


async def load_risk_profile(
    session: AsyncSession, user_id: uuid.UUID
) -> RiskProfile | None:
    result = await session.execute(
        select(RiskProfile).where(RiskProfile.user_id == user_id)
    )
    return result.scalar_one_or_none()


async def load_broker_account(
    session: AsyncSession, user_id: uuid.UUID, label: str
) -> BrokerAccount | None:
    result = await session.execute(
        select(BrokerAccount).where(
            BrokerAccount.user_id == user_id,
            BrokerAccount.label == label,
            BrokerAccount.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def load_instrument(
    session: AsyncSession, broker: str, symbol: str, now: datetime
) -> InstrumentSpec | None:
    """Load cached exchange filters, rejecting anything too stale (OQ-5)."""
    result = await session.execute(
        select(Instrument).where(
            Instrument.broker == broker, Instrument.symbol == symbol
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None

    age = (now - row.fetched_at).total_seconds()
    if age > MAX_INSTRUMENT_AGE_SEC:
        logger.warning(
            "Instrument filters are stale",
            extra={"symbol": symbol, "age_sec": int(age)},
        )
        return None

    return InstrumentSpec(
        symbol=row.symbol,
        tick_size=row.tick_size,
        lot_step=row.lot_step,
        min_qty=row.min_qty,
        min_notional=row.min_notional,
    )


async def load_circuit_breaker(
    session: AsyncSession, account_id: uuid.UUID
) -> CircuitBreakerSnapshot:
    """Read durable breaker state.

    A missing row means the breaker has never tripped for this account, which is
    a genuine CLOSED — not an unknown. The unknown case is Redis being down, and
    that is handled by the caller before we get here.
    """
    result = await session.execute(
        select(CircuitBreakerState).where(
            CircuitBreakerState.broker_account_id == account_id
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return CircuitBreakerSnapshot()
    return CircuitBreakerSnapshot(
        state=CircuitState(row.state),
        consecutive_losses=row.consecutive_losses,
        cooldown_until=row.cooldown_until,
    )


async def load_drawdown(
    session: AsyncSession,
    account_id: uuid.UUID,
    profile: RiskProfile,
    now: datetime,
    current_equity: Decimal,
) -> DrawdownSnapshot:
    """Find today's baseline and whether the limit has already been breached.

    `tripped_today` is derived by asking whether *any* equity reading in this
    session already breached the limit — not just the current one. That is what
    makes the block persist after a recovery, and it survives a restart because
    it is recomputed from stored snapshots rather than remembered in memory.
    """
    today = session_date_for(now, profile.daily_reset_time, profile.timezone)

    baseline_result = await session.execute(
        select(EquitySnapshot).where(
            EquitySnapshot.broker_account_id == account_id,
            EquitySnapshot.is_session_baseline.is_(True),
            EquitySnapshot.session_date == today,
        )
    )
    baseline_row = baseline_result.scalar_one_or_none()

    if baseline_row is None:
        # No baseline yet for this session: the current equity becomes it. The
        # caller persists it; a drawdown of zero cannot trip the limit.
        return DrawdownSnapshot(baseline_equity=current_equity, tripped_today=False)

    baseline = baseline_row.equity
    if baseline <= 0:
        return DrawdownSnapshot(baseline_equity=baseline, tripped_today=False)

    limit_equity = baseline * (Decimal("1") - profile.max_daily_dd_pct)
    breached = await session.execute(
        select(EquitySnapshot.id)
        .where(
            EquitySnapshot.broker_account_id == account_id,
            EquitySnapshot.taken_at >= baseline_row.taken_at,
            EquitySnapshot.equity <= limit_equity,
        )
        .limit(1)
    )
    return DrawdownSnapshot(
        baseline_equity=baseline, tripped_today=breached.scalar_one_or_none() is not None
    )


def build_alert_input(payload_data: dict[str, object], dedupe_key: str) -> AlertInput:
    """Convert a validated payload into the engine's input type."""
    return AlertInput(
        symbol=str(payload_data["symbol"]),
        action=AlertAction(str(payload_data["action"])),
        order_type=OrderType(str(payload_data["order_type"])),
        timestamp=payload_data["timestamp"],  # type: ignore[arg-type]
        dedupe_key=dedupe_key,
        limit_price=payload_data.get("limit_price"),  # type: ignore[arg-type]
        stop_price=payload_data.get("stop_price"),  # type: ignore[arg-type]
        take_profit=payload_data.get("take_profit"),  # type: ignore[arg-type]
    )


def account_state_from(
    account: BrokerAccount,
    equity: Decimal,
    free_balance: Decimal,
    positions: tuple[OpenPosition, ...],
) -> AccountState:
    position_value = sum((p.notional for p in positions), start=Decimal("0"))
    return AccountState(
        trading_state=TradingState(account.trading_state),
        total_equity=equity,
        free_balance=free_balance,
        position_value=position_value,
        positions=positions,
    )


async def persist_decision(
    session: AsyncSession,
    *,
    alert_id: uuid.UUID,
    decision: Decision,
    broker_account_id: uuid.UUID | None,
    profile_version: int,
    evaluated_at: datetime,
    latency_ms: int,
    is_test: bool,
) -> uuid.UUID:
    """Write the decision. Append-only — never updated afterwards."""
    row = DecisionRow(
        id=uuid.uuid4(),
        alert_id=alert_id,
        broker_account_id=broker_account_id,
        verdict=decision.verdict.value,
        reason_code=decision.reason_code.value,
        reason_detail=decision.reason_detail,
        rule_snapshot=decision.rule_snapshot,
        risk_profile_version=profile_version,
        computed_qty=decision.computed_qty,
        entry_reference_price=decision.entry_reference_price,
        stop_price=decision.stop_price,
        evaluated_at=evaluated_at,
        latency_ms=latency_ms,
        is_test=is_test,
    )
    session.add(row)
    await session.flush()
    return row.id


async def evaluate_alert(
    session: AsyncSession,
    *,
    alert: Alert,
    payload_data: dict[str, object] | None,
    payload_error: str | None,
    is_duplicate: bool,
    account_state_provider: AccountStateProvider,
    now: datetime,
    market_reference_price: Decimal | None = None,
    is_test: bool = False,
) -> PipelineResult:
    """Run the full pipeline for one alert and persist the decision.

    `account_state_provider` is an async callable returning
    `(equity, free_balance, positions)` for a broker account, or raising to
    signal the broker is unreachable. Injecting it keeps this function testable
    with a fake and keeps broker knowledge out of the ingress layer.
    """
    started = datetime.now(UTC)

    profile = await load_risk_profile(session, alert.user_id)
    if profile is None:
        decision = _pipeline_rejection(
            None, ReasonCode.ACCOUNT_NOT_FOUND, "no risk profile configured"
        )
        return await _finish(session, alert, decision, None, 0, now, started, is_test)

    config = profile_to_config(profile)

    if payload_data is None:
        decision = evaluate(
            Snapshot(
                now=now,
                config=config,
                circuit=CircuitBreakerSnapshot(),
                drawdown=DrawdownSnapshot(baseline_equity=Decimal("0")),
                alert=None,
                payload_error=payload_error,
            )
        )
        return await _finish(
            session, alert, decision, None, profile.version, now, started, is_test
        )

    account = await load_broker_account(
        session, alert.user_id, str(payload_data["account"])
    )
    if account is None:
        decision = _pipeline_rejection(
            config,
            ReasonCode.ACCOUNT_NOT_FOUND,
            f"no active broker account labelled {payload_data['account']!r}",
        )
        return await _finish(
            session, alert, decision, None, profile.version, now, started, is_test
        )

    # Broker state. If this fails we cannot build a snapshot, so we reject
    # rather than queue (plan §3, Q2) — a queued signal would fire later at a
    # price the strategy never saw.
    try:
        equity, free_balance, positions = await account_state_provider(account)
    except Exception as exc:  # noqa: BLE001 - any broker failure means reject
        logger.error(
            "Broker unreachable during evaluation",
            extra={"error": type(exc).__name__, "account_label": account.label},
        )
        decision = _pipeline_rejection(
            config, ReasonCode.BROKER_UNAVAILABLE, "broker state unavailable"
        )
        return await _finish(
            session, alert, decision, account.id, profile.version, now, started, is_test
        )

    instrument = await load_instrument(
        session, account.broker, str(payload_data["symbol"]), now
    )
    if instrument is None:
        decision = _pipeline_rejection(
            config,
            ReasonCode.INSTRUMENT_UNAVAILABLE,
            f"no fresh exchange filters for {payload_data['symbol']}",
        )
        return await _finish(
            session, alert, decision, account.id, profile.version, now, started, is_test
        )

    breaker = await load_circuit_breaker(session, account.id)
    drawdown = await load_drawdown(session, account.id, profile, now, equity)

    alert_input = build_alert_input(payload_data, alert.dedupe_key)

    # A limit order carries its own price. A market order does not, so sizing
    # needs a reference price fetched at evaluation time (plan §2, A8) — supplied
    # by the caller, because fetching it is broker work and this layer knows no
    # brokers. Without one, sizing has nothing to work from and rule 9 rejects.
    reference_price = (
        alert_input.limit_price
        if alert_input.order_type is OrderType.LIMIT
        else market_reference_price
    )

    snapshot = Snapshot(
        now=now,
        config=config,
        circuit=breaker,
        drawdown=drawdown,
        alert=alert_input,
        is_duplicate=is_duplicate,
        account=account_state_from(account, equity, free_balance, positions),
        instrument=instrument,
        reference_price=reference_price,
    )
    decision = evaluate(snapshot)
    return await _finish(
        session, alert, decision, account.id, profile.version, now, started, is_test
    )


async def _finish(
    session: AsyncSession,
    alert: Alert,
    decision: Decision,
    account_id: uuid.UUID | None,
    profile_version: int,
    now: datetime,
    started: datetime,
    is_test: bool,
) -> PipelineResult:
    latency_ms = max(0, int((datetime.now(UTC) - started).total_seconds() * 1000))
    decision_id = await persist_decision(
        session,
        alert_id=alert.id,
        decision=decision,
        broker_account_id=account_id,
        profile_version=profile_version,
        evaluated_at=now,
        latency_ms=latency_ms,
        is_test=is_test,
    )
    return PipelineResult(
        decision=decision, decision_id=decision_id, latency_ms=latency_ms
    )
