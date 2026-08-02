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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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

# Supplies the current market price for sizing a MARKET order. Injected for the
# same reason as the state provider: fetching a price is broker work, and this
# layer knows no brokers. Returning None means "no price available", which the
# rule chain turns into a rejection rather than a guess.
ReferencePriceProvider = Callable[[BrokerAccount, str], Awaitable[Decimal | None]]


@dataclass(frozen=True)
class PipelineResult:
    """The outcome of evaluating one alert.

    `account` and `alert_input` are carried out so the caller can execute an
    approval without re-resolving what the pipeline already resolved. They are
    None on the paths where evaluation stopped before they existed.
    """

    decision: Decision
    decision_id: uuid.UUID
    latency_ms: int
    account: BrokerAccount | None = None
    alert_input: AlertInput | None = None


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
    free_balance: Decimal | None = None,
    persist_baseline: bool = True,
) -> DrawdownSnapshot:
    """Find today's baseline and whether the limit has already been breached.

    `tripped_today` is derived by asking whether *any* equity reading in this
    session already breached the limit — not just the current one. That is what
    makes the block persist after a recovery, and it survives a restart because
    it is recomputed from stored snapshots rather than remembered in memory.

    `persist_baseline=False` for **test** evaluations. A `/test` run works from a
    simulated equity figure, and writing that as the day's baseline would let a
    what-if question silently set the number every real drawdown check for the
    rest of the session is measured against — either blocking live trading or
    hiding a genuine drawdown. A simulation must never leave a mark on the state
    the risk engine reads.
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
        # No baseline yet for this session, so the current equity becomes it and
        # is persisted here and now. Returning a baseline without writing it (the
        # original behaviour) meant the *next* alert looked it up, found nothing
        # again, and re-baselined against whatever equity had become — which
        # quietly makes a daily drawdown limit unable to ever trip.
        if not persist_baseline:
            return DrawdownSnapshot(
                baseline_equity=current_equity, tripped_today=False
            )
        try:
            # A SAVEPOINT, not the outer transaction: a lost race for the
            # baseline must not take the alert row down with it.
            async with session.begin_nested():
                session.add(
                    EquitySnapshot(
                        id=uuid.uuid4(),
                        broker_account_id=account_id,
                        equity=current_equity,
                        free_balance=free_balance,
                        taken_at=now,
                        is_session_baseline=True,
                        session_date=today,
                    )
                )
        except IntegrityError:
            # The partial unique index did its job: a concurrent alert created
            # the baseline first. Theirs is as valid as ours would have been.
            logger.debug("Session baseline already created concurrently")
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
    reference_price_provider: ReferencePriceProvider | None = None,
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

    # Exchange filters are only needed to *size* a trade, and a symbol the user
    # has not allowed will never be sized — rule 5 short-circuits long before
    # rule 9. Fetching first meant an un-allowed symbol was refused with
    # INSTRUMENT_UNAVAILABLE, which names a fixable-looking infrastructure
    # problem instead of the user's actual mistake, and sends them debugging
    # the wrong thing (F-1). The engine still produces every verdict; this only
    # decides whether we bother doing a lookup whose answer cannot matter.
    symbol = str(payload_data["symbol"])
    instrument: InstrumentSpec | None = None
    if symbol in config.allowed_symbols:
        instrument = await load_instrument(session, account.broker, symbol, now)
        if instrument is None:
            decision = _pipeline_rejection(
                config,
                ReasonCode.INSTRUMENT_UNAVAILABLE,
                f"no fresh exchange filters for {symbol}",
            )
            return await _finish(
                session, alert, decision, account.id, profile.version, now,
                started, is_test,
            )

    breaker = await load_circuit_breaker(session, account.id)
    drawdown = await load_drawdown(
        session, account.id, profile, now, equity, free_balance,
        persist_baseline=not is_test,
    )

    alert_input = build_alert_input(payload_data, alert.dedupe_key)

    # A limit order carries its own price. A market order does not, so sizing
    # needs a reference price fetched at evaluation time (plan §2, A8) — supplied
    # by the caller, because fetching it is broker work and this layer knows no
    # brokers. Without one, sizing has nothing to work from and rule 6 rejects.
    if alert_input.order_type is OrderType.LIMIT:
        reference_price = alert_input.limit_price
    elif reference_price_provider is not None:
        reference_price = await reference_price_provider(
            account, alert_input.symbol
        )
    else:
        reference_price = None

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
    decision = _clarify_missing_price(decision, alert_input, reference_price)
    return await _finish(
        session,
        alert,
        decision,
        account.id,
        profile.version,
        now,
        started,
        is_test,
        account=account,
        alert_input=alert_input,
    )


def _clarify_missing_price(
    decision: Decision, alert: AlertInput, reference_price: Decimal | None
) -> Decision:
    """Rename a rejection that blames the stop when the real cause was no price.

    Rule 6 cannot validate a stop without a price to validate it against, so it
    rejects — correctly. But it reports `NO_STOP_LOSS`, which tells a user their
    stop is wrong when the stop was fine and the price lookup was what failed.
    They then go and "fix" a correct stop (F-1).

    **The verdict is untouched — only the label.** This runs *after* the engine
    precisely so §7's ordering still decides everything: a locked account, an
    invalid payload, a stale or duplicate alert, a disallowed symbol all
    short-circuit earlier and are never relabelled. We only get here when rules
    1-5 passed and rule 6 was the first to object.
    """
    if decision.reason_code is not ReasonCode.NO_STOP_LOSS:
        return decision
    if reference_price is not None or alert.order_type is not OrderType.MARKET:
        return decision
    return replace(
        decision,
        reason_code=ReasonCode.PRICE_UNAVAILABLE,
        reason_detail=(
            "no market price available to size this order or validate its stop; "
            "the stop itself was not the problem"
        ),
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
    *,
    account: BrokerAccount | None = None,
    alert_input: AlertInput | None = None,
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
        decision=decision,
        decision_id=decision_id,
        latency_ms=latency_ms,
        account=account,
        alert_input=alert_input,
    )
