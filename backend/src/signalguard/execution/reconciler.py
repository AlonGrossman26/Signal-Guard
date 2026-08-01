"""Reconciliation: the broker is the truth, our tables are a cache (CLAUDE.md §10).

Runs on a loop and repairs local state from broker ground truth. It exists
because every other part of this system can be wrong: a response can be lost
after the exchange accepted the order, a fill can arrive while we believed the
order failed, a process can die between two writes.

It also does the job described in plan §3 Q1: on a LOCKED account, **any** open
order or non-flat position is a violation and gets flattened. That is what turns
the kill switch from a one-time action into a continuously-enforced state, and
it is what closes the in-flight-order race without needing to win it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import BrokerAccount, EquitySnapshot, Position
from signalguard.db.models import Order as OrderRow
from signalguard.enums import OrderStatus, TradingState
from signalguard.execution.base import (
    AccountState as BrokerAccountState,
)
from signalguard.execution.base import (
    BrokerAdapter,
    BrokerError,
    BrokerPosition,
)
from signalguard.realtime import EventType, publish
from signalguard.risk.session import session_date_for

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 15

# Injected rather than imported, so the loop can be driven with a fake broker and
# a throwaway session in tests without reaching for a network or a real account.
SessionFactory = Callable[[], AsyncIterator[AsyncSession]]
BrokerFactory = Callable[[BrokerAccount], Awaitable[BrokerAdapter]]

# A bound sink that fans a reconciled change out to one user's dashboard feed.
# Optional everywhere: when absent, reconciliation is exactly as before, and the
# durable tables are still the source of truth (constraint #5).
EventSink = Callable[[EventType, dict[str, Any]], Awaitable[None]]

# Statuses that may still change at the exchange, so they are worth asking about.
_OPEN_STATUSES = (
    OrderStatus.PENDING_SUBMIT.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.UNKNOWN.value,
)


@dataclass
class ReconcileReport:
    orders_repaired: int = 0
    positions_synced: int = 0
    lock_violations_flattened: int = 0
    errors: list[str] = field(default_factory=list)


async def reconcile_account(
    session: AsyncSession,
    broker: BrokerAdapter,
    account: BrokerAccount,
    now: datetime,
    *,
    event_sink: EventSink | None = None,
) -> ReconcileReport:
    """Pull broker truth and repair local state for one account.

    When `event_sink` is supplied, every repaired order, synced position and
    equity tick is also fanned out to the dashboard's live feed. It is optional
    so the loop, and every existing test, can run without a Redis publisher —
    the persisted rows are the record; the events are a live convenience on top.
    """
    report = ReconcileReport()

    # --- Orders ---------------------------------------------------------------
    # Every order we think is still open gets checked by its client_order_id.
    # PENDING_SUBMIT rows matter most: they are the ones where we sent a request
    # and never learned the outcome.
    result = await session.execute(
        select(OrderRow).where(
            OrderRow.broker_account_id == account.id,
            OrderRow.status.in_(_OPEN_STATUSES),
        )
    )
    for row in result.scalars():
        try:
            truth = await broker.get_order(row.client_order_id, row.symbol)
        except BrokerError as exc:
            if exc.code.value == "UNKNOWN_ORDER":
                # The exchange has never heard of it, so the submission never
                # landed. Safe to mark rejected: the client ID means it cannot
                # reappear later as a surprise.
                row.status = OrderStatus.REJECTED.value
                row.last_error = "not found at broker"
                report.orders_repaired += 1
                await _emit_order(event_sink, account, row)
            else:
                report.errors.append(f"order:{exc.code.value}")
            continue

        if (
            row.status != truth.status.value
            or row.filled_qty != truth.filled_qty
        ):
            row.status = truth.status.value
            row.broker_order_id = truth.broker_order_id
            row.filled_qty = truth.filled_qty
            row.avg_fill_price = truth.avg_fill_price
            row.fees = truth.fees
            row.fee_asset = truth.fee_asset
            if truth.status is OrderStatus.FILLED:
                row.filled_at = truth.updated_at or now
            report.orders_repaired += 1
            await _emit_order(event_sink, account, row)

    # --- Positions and equity -------------------------------------------------
    try:
        state = await broker.get_account_state()
    except BrokerError as exc:
        report.errors.append(f"account_state:{exc.code.value}")
        await session.flush()
        return report

    await _sync_positions(session, account.id, state.positions)
    report.positions_synced = len(state.positions)
    if event_sink is not None:
        for position in state.positions:
            await event_sink(
                EventType.POSITION,
                {
                    "symbol": position.symbol,
                    "qty": position.qty,
                    "avg_entry": position.avg_entry,
                    "mark_price": position.mark_price,
                    "unrealized_pnl": (position.mark_price - position.avg_entry)
                    * position.qty,
                },
            )

    await _record_equity(session, account, state.total_equity, state.free_balance,
                         state.position_value, now)
    if event_sink is not None:
        await event_sink(
            EventType.EQUITY,
            {
                "equity": state.total_equity,
                "free_balance": state.free_balance,
                "position_value": state.position_value,
                "taken_at": now,
            },
        )

    # --- Enforce the lock -----------------------------------------------------
    if account.trading_state == TradingState.LOCKED.value:
        flattened = await _enforce_lock(broker, state)
        report.lock_violations_flattened = flattened
        if flattened:
            logger.critical(
                "Reconciler flattened positions on a LOCKED account",
                extra={"broker_account_id": str(account.id), "count": flattened},
            )

    await session.flush()
    return report


async def _emit_order(
    event_sink: EventSink | None, account: BrokerAccount, row: OrderRow
) -> None:
    """Fan a repaired order's new state out to the dashboard, if a sink is set."""
    if event_sink is None:
        return
    await event_sink(
        EventType.ORDER,
        {
            "order_id": row.id,
            "symbol": row.symbol,
            "side": row.side,
            "type": row.type,
            "role": row.role,
            "status": row.status,
            "qty": row.qty,
            "filled_qty": row.filled_qty,
            "avg_fill_price": row.avg_fill_price,
        },
    )


async def _sync_positions(
    session: AsyncSession,
    account_id: uuid.UUID,
    positions: tuple[BrokerPosition, ...],
) -> None:
    """Overwrite local positions from broker truth, dropping anything gone."""
    existing = await session.execute(
        select(Position).where(Position.broker_account_id == account_id)
    )
    by_symbol = {row.symbol: row for row in existing.scalars()}

    for position in positions:
        row = by_symbol.pop(position.symbol, None)
        if row is None:
            session.add(
                Position(
                    id=uuid.uuid4(),
                    broker_account_id=account_id,
                    symbol=position.symbol,
                    qty=position.qty,
                    avg_entry=position.avg_entry,
                    mark_price=position.mark_price,
                    unrealized_pnl=(position.mark_price - position.avg_entry)
                    * position.qty,
                )
            )
        else:
            row.qty = position.qty
            row.avg_entry = position.avg_entry
            row.mark_price = position.mark_price
            row.unrealized_pnl = (position.mark_price - position.avg_entry) * position.qty

    # Anything the broker no longer reports is gone. Local state is a cache, so
    # a stale row here would be a position we believe in that does not exist.
    for orphan in by_symbol.values():
        orphan.qty = Decimal("0")
        orphan.unrealized_pnl = Decimal("0")


async def _record_equity(
    session: AsyncSession,
    account: BrokerAccount,
    equity: Decimal,
    free_balance: Decimal,
    position_value: Decimal,
    now: datetime,
) -> None:
    """Append an equity tick, creating the session baseline if today has none.

    The baseline is created lazily on the first reading of a new trading day.
    The partial unique index makes that safe under concurrency and across a
    restart: a second attempt for the same session_date simply loses.
    """
    profile_result = await session.execute(
        select(BrokerAccount).where(BrokerAccount.id == account.id)
    )
    if profile_result.scalar_one_or_none() is None:
        return

    from signalguard.db.models import RiskProfile

    profile = (
        await session.execute(
            select(RiskProfile).where(RiskProfile.user_id == account.user_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return

    today = session_date_for(now, profile.daily_reset_time, profile.timezone)
    baseline_exists = (
        await session.execute(
            select(EquitySnapshot.id).where(
                EquitySnapshot.broker_account_id == account.id,
                EquitySnapshot.is_session_baseline.is_(True),
                EquitySnapshot.session_date == today,
            )
        )
    ).scalar_one_or_none()

    session.add(
        EquitySnapshot(
            id=uuid.uuid4(),
            broker_account_id=account.id,
            equity=equity,
            free_balance=free_balance,
            position_value=position_value,
            taken_at=now,
            is_session_baseline=baseline_exists is None,
            session_date=today,
        )
    )


async def _enforce_lock(broker: BrokerAdapter, state: BrokerAccountState) -> int:
    """A LOCKED account must be flat. Anything open is flattened."""
    open_positions = [p for p in state.positions if p.qty != 0]
    if not open_positions:
        return 0
    try:
        await broker.cancel_all_orders()
        closed = await broker.close_all_positions()
        return len(closed)
    except BrokerError as exc:
        logger.critical(
            "Could not flatten a LOCKED account; will retry next cycle",
            extra={"error_code": exc.code.value},
        )
        return 0


def _sink_for(redis: Any, account: BrokerAccount) -> EventSink:
    """Bind a publisher to one account's owner, ready to hand to reconcile."""
    user_id = account.user_id

    async def sink(event_type: EventType, data: dict[str, Any]) -> None:
        await publish(redis, user_id, event_type, data)

    return sink


async def reconciliation_loop(
    session_factory: SessionFactory,
    broker_factory: BrokerFactory,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    *,
    stop_event: asyncio.Event | None = None,
    redis: Any | None = None,
) -> None:
    """Reconcile every active account, forever.

    Failures are logged and the loop continues. A reconciler that dies on the
    first error is worse than none at all: it stops repairing state precisely
    when state is most likely to be wrong.

    When `redis` is supplied, each account's repaired orders, positions and
    equity are published to that account owner's dashboard channel. Without it
    the loop reconciles silently, exactly as before.
    """
    while stop_event is None or not stop_event.is_set():
        try:
            async for session in session_factory():
                accounts = await session.execute(
                    select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
                )
                for account in accounts.scalars():
                    broker = await broker_factory(account)
                    sink = _sink_for(redis, account) if redis is not None else None
                    await reconcile_account(
                        session, broker, account, datetime.now(UTC), event_sink=sink
                    )
                await session.commit()
        except Exception:
            logger.exception("Reconciliation cycle failed; continuing")

        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            else:
                await asyncio.sleep(interval_sec)
        except TimeoutError:
            continue
