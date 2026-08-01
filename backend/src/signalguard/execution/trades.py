"""Closed round-trips and the circuit-breaker state they drive (CLAUDE.md §6, §7).

Two jobs, in order:

1. **Build `trades` from filled orders.** A trade is a completed round-trip, not
   an order. On spot that means: an ENTRY order that filled, and a later STOP or
   EXIT order on the same decision that filled. Both carry the same `decision_id`,
   which is what lets an exit be matched back to the entry that opened it without
   guessing.

2. **Recompute the circuit breaker from those trades.** §7 rule 7 counts *closed*
   trades with realized PnL < 0, consecutively, most recent first. The counting
   itself is pure and already lives in `risk/breaker.py`; this module supplies it
   with rows and persists the answer.

**Why recompute rather than increment.** An incrementing counter has to be
exactly-once or it drifts, and "exactly once" across a crash between two writes
is the hardest guarantee in this system to get right. Recomputing from the trade
table is idempotent by construction: run it twice, get the same number. That
makes a restart mid-cycle boring, which is the property worth having in the one
rule that decides whether a losing streak can keep losing.

Fees are subtracted from realized PnL. A trade that is gross-positive but
fee-negative is a loss, and the breaker must count it as one — the `Trade` model
says so explicitly, and a user who is bleeding to fees is exactly who the breaker
exists to stop.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import BrokerAccount, CircuitBreakerState, RiskProfile, Trade
from signalguard.db.models import Order as OrderRow
from signalguard.enums import CircuitState, OrderRole, OrderSide, OrderStatus
from signalguard.risk.breaker import count_consecutive_losses

logger = logging.getLogger(__name__)

# How far back the streak count reads. The breaker only ever cares about the
# current run of losses, so a bounded window keeps the query cheap; no realistic
# threshold reaches this far.
_STREAK_LOOKBACK = 100

# Orders that close a position. TAKE_PROFIT is included for completeness — it is
# not submitted in v1, but if it ever is, it closes a round-trip like any other.
_CLOSING_ROLES = (OrderRole.STOP.value, OrderRole.EXIT.value, OrderRole.TAKE_PROFIT.value)

_BREAKER_KEY = "sg:breaker:"


@dataclass(frozen=True)
class BreakerUpdate:
    """The breaker state after a recount, and whether this run tripped it."""

    state: CircuitState
    consecutive_losses: int
    cooldown_until: datetime | None
    newly_opened: bool


async def build_trades(
    session: AsyncSession, account: BrokerAccount, now: datetime
) -> list[Trade]:
    """Turn newly-filled closing orders into `trades` rows. Returns what it made.

    Idempotent: an exit order already referenced by a trade is skipped, so
    running this on every reconciliation cycle produces each round-trip once.
    """
    already_recorded = await session.execute(
        select(Trade.exit_order_id).where(Trade.broker_account_id == account.id)
    )
    recorded = {row for row in already_recorded.scalars() if row is not None}

    closing = await session.execute(
        select(OrderRow).where(
            OrderRow.broker_account_id == account.id,
            OrderRow.role.in_(_CLOSING_ROLES),
            OrderRow.status == OrderStatus.FILLED.value,
        )
    )

    created: list[Trade] = []
    for exit_order in closing.scalars():
        if exit_order.id in recorded:
            continue

        entry_order = (
            await session.execute(
                select(OrderRow).where(
                    OrderRow.decision_id == exit_order.decision_id,
                    OrderRow.role == OrderRole.ENTRY.value,
                )
            )
        ).scalar_one_or_none()

        if entry_order is None or entry_order.avg_fill_price is None:
            # An exit with no identifiable entry cannot be priced into a PnL.
            # Skipping is correct: inventing an entry price would put a fictional
            # number in front of the circuit breaker, and the breaker is the one
            # thing that must never be fed a guess.
            logger.warning(
                "Closing order has no priced entry; not recording a trade",
                extra={"order_id": str(exit_order.id), "symbol": exit_order.symbol},
            )
            continue
        if exit_order.avg_fill_price is None:
            continue

        qty = exit_order.filled_qty
        if qty <= 0:
            continue

        entry_price = entry_order.avg_fill_price
        exit_price = exit_order.avg_fill_price
        fees = entry_order.fees + exit_order.fees

        # v1 is spot long-only (OQ-6): the entry is a BUY and the exit a SELL, so
        # profit is (exit - entry). The short branch is written out anyway so the
        # arithmetic is not silently wrong the day a futures adapter arrives.
        if entry_order.side == OrderSide.BUY.value:
            gross = (exit_price - entry_price) * qty
        else:
            gross = (entry_price - exit_price) * qty

        trade = Trade(
            id=uuid.uuid4(),
            broker_account_id=account.id,
            symbol=exit_order.symbol,
            side=entry_order.side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            realized_pnl=gross - fees,
            fees=fees,
            opened_at=entry_order.filled_at or entry_order.submitted_at,
            closed_at=exit_order.filled_at or now,
            entry_order_id=entry_order.id,
            exit_order_id=exit_order.id,
        )
        session.add(trade)
        created.append(trade)

    if created:
        await session.flush()
        logger.info(
            "Recorded closed trades",
            extra={
                "broker_account_id": str(account.id),
                "count": len(created),
                "losses": sum(1 for t in created if t.realized_pnl < 0),
            },
        )
    return created


async def recompute_circuit_breaker(
    session: AsyncSession,
    account: BrokerAccount,
    now: datetime,
    *,
    redis: Any | None = None,
) -> BreakerUpdate | None:
    """Recount the losing streak and persist the breaker state.

    Returns None when the account's owner has no risk profile (nothing to compare
    a threshold against). `newly_opened` is True only on the transition into OPEN,
    so a caller can alert once rather than on every cycle while it stays open.
    """
    profile = (
        await session.execute(
            select(RiskProfile).where(RiskProfile.user_id == account.user_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        return None

    pnls = (
        await session.execute(
            select(Trade.realized_pnl)
            .where(Trade.broker_account_id == account.id)
            .order_by(Trade.closed_at.desc())
            .limit(_STREAK_LOOKBACK)
        )
    ).scalars().all()

    streak = count_consecutive_losses(list(pnls))

    existing = (
        await session.execute(
            select(CircuitBreakerState).where(
                CircuitBreakerState.broker_account_id == account.id
            )
        )
    ).scalar_one_or_none()
    was_open = existing is not None and existing.state == CircuitState.OPEN.value

    newest_trade_id = (
        await session.execute(
            select(Trade.id)
            .where(Trade.broker_account_id == account.id)
            .order_by(Trade.closed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if streak >= profile.consecutive_loss_threshold:
        state = CircuitState.OPEN
        # A manual-reset profile records no cooldown at all. `is_blocking` reads
        # a missing cooldown as "blocked until something clears it explicitly",
        # which is the fail-closed reading and the one the user asked for.
        cooldown_until = (
            None
            if profile.circuit_breaker_manual_reset
            else now + timedelta(minutes=profile.circuit_breaker_cooldown_minutes)
        )
        # Keep the original trip time and cooldown while it is already open —
        # re-arming the cooldown every cycle would make it never expire.
        opened_at = existing.opened_at if was_open and existing is not None else now
        if was_open and existing is not None:
            cooldown_until = existing.cooldown_until
    else:
        state = CircuitState.CLOSED
        cooldown_until = None
        opened_at = None

    await session.execute(
        pg_insert(CircuitBreakerState)
        .values(
            broker_account_id=account.id,
            state=state.value,
            consecutive_losses=streak,
            opened_at=opened_at,
            cooldown_until=cooldown_until,
            last_counted_trade_id=newest_trade_id,
        )
        .on_conflict_do_update(
            index_elements=["broker_account_id"],
            set_={
                "state": state.value,
                "consecutive_losses": streak,
                "opened_at": opened_at,
                "cooldown_until": cooldown_until,
                "last_counted_trade_id": newest_trade_id,
            },
        )
    )
    await session.flush()

    newly_opened = state is CircuitState.OPEN and not was_open
    if newly_opened:
        logger.critical(
            "Circuit breaker OPENED",
            extra={
                "broker_account_id": str(account.id),
                "consecutive_losses": streak,
                "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
            },
        )

    await _mirror_to_redis(redis, account.id, state, streak)

    return BreakerUpdate(
        state=state,
        consecutive_losses=streak,
        cooldown_until=cooldown_until,
        newly_opened=newly_opened,
    )


async def _mirror_to_redis(
    redis: Any | None, account_id: uuid.UUID, state: CircuitState, streak: int
) -> None:
    """Publish the breaker state to Redis, the fast path (§7).

    Postgres is the record; this is a cache. A failure here is logged and
    swallowed — the durable copy is already written, and the read path falls back
    to it. That is the whole reason §7 demands the state live in both.
    """
    if redis is None:
        return
    try:
        await redis.hset(
            f"{_BREAKER_KEY}{account_id}",
            mapping={"state": state.value, "consecutive_losses": str(streak)},
        )
    except RedisError:
        logger.warning(
            "Could not cache circuit-breaker state in Redis; Postgres is authoritative",
            extra={"broker_account_id": str(account_id)},
        )


async def settle_account(
    session: AsyncSession,
    account: BrokerAccount,
    now: datetime | None = None,
    *,
    redis: Any | None = None,
) -> tuple[list[Trade], BreakerUpdate | None]:
    """Build trades, then recount the breaker. The pair always runs together.

    Separating them would allow a state where a loss is recorded but not counted,
    and a breaker that is one trade behind is a breaker that lets the trade it
    should have blocked through.
    """
    moment = now or datetime.now(UTC)
    trades = await build_trades(session, account, moment)
    update = await recompute_circuit_breaker(session, account, moment, redis=redis)
    return trades, update
