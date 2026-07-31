"""The kill switch (CLAUDE.md §10, plan §3 Q1).

Sequence: **lock first, then sweep.** Locking before cancelling means no new
order can be accepted while the sweep runs — otherwise the risk engine could
approve a signal into an account we are in the middle of flattening.

The design decision that matters (plan §3, Q1): **LOCKED is a state, not an
event.** An in-flight submission cannot be recalled — you cannot un-send an HTTP
request, and abandoning it loses the order ID, which is how a position becomes
invisible to us. So instead of trying to win that race, the sweep runs twice and
the reconciliation loop treats any open order or non-flat position on a LOCKED
account as a violation to be flattened, continuously. The invariant is enforced
until reality matches it.

The endpoint is idempotent: firing it twice is harmless, and firing it on an
already-locked account still re-sweeps. It must also work when Redis is down,
so the durable lock lives in Postgres and Redis is updated opportunistically.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import BrokerAccount
from signalguard.enums import TradingState
from signalguard.execution.base import BrokerAdapter, BrokerError

logger = logging.getLogger(__name__)

# Seconds between the two sweeps. Long enough for an in-flight submission to
# land at the exchange and become visible, short enough to stay responsive.
SETTLE_SECONDS = 2.0

_LOCK_KEY = "sg:locked:"


@dataclass
class KillSwitchResult:
    account_id: uuid.UUID
    orders_cancelled: int = 0
    positions_closed: int = 0
    locked: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return self.locked and not self.errors


async def set_locked(
    session: AsyncSession,
    redis: Redis | None,
    account_id: uuid.UUID,
    reason: str,
) -> None:
    """Write the lock durably, then cache it.

    Postgres is the record and Redis is the fast path. If Redis is unavailable
    the lock still holds, because the read path falls back to Postgres — which
    is exactly why `trading_state` is a column rather than Redis-only state.
    """
    await session.execute(
        update(BrokerAccount)
        .where(BrokerAccount.id == account_id)
        .values(
            trading_state=TradingState.LOCKED.value,
            locked_at=datetime.now(UTC),
            locked_reason=reason,
        )
    )
    await session.flush()

    if redis is not None:
        try:
            await redis.set(f"{_LOCK_KEY}{account_id}", "1")
        except RedisError:
            # The durable lock is already written; the cache will catch up.
            logger.warning("Could not cache kill-switch lock in Redis")


async def is_locked(
    session: AsyncSession, redis: Redis | None, account_id: uuid.UUID
) -> bool:
    """Read the lock: Redis first, Postgres on failure, fail closed if neither.

    An unanswerable "is this account locked?" is treated as LOCKED. Assuming
    unlocked would mean trading an account we cannot confirm is safe.
    """
    if redis is not None:
        try:
            cached = await redis.get(f"{_LOCK_KEY}{account_id}")
            if cached is not None:
                return True
        except RedisError:
            logger.warning("Redis unavailable for lock check; falling back to Postgres")

    result = await session.execute(
        select(BrokerAccount.trading_state).where(BrokerAccount.id == account_id)
    )
    state = result.scalar_one_or_none()
    if state is None:
        return True  # unknown account is not a tradeable account
    return state == TradingState.LOCKED.value


async def fire_kill_switch(
    session: AsyncSession,
    redis: Redis | None,
    broker: BrokerAdapter,
    account_id: uuid.UUID,
    reason: str = "manual kill switch",
) -> KillSwitchResult:
    """Lock the account, then flatten it. Idempotent."""
    result = KillSwitchResult(account_id=account_id)

    # 1. Lock FIRST, so nothing new can be approved while we sweep.
    await set_locked(session, redis, account_id, reason)
    result.locked = True
    logger.critical(
        "Kill switch fired", extra={"broker_account_id": str(account_id), "reason": reason}
    )

    # 2. Two sweeps. An order that lands during the first is caught by the
    #    second — the race is not won, it is outlasted.
    for pass_number in (1, 2):
        try:
            result.orders_cancelled += await broker.cancel_all_orders()
        except BrokerError as exc:
            result.errors.append(f"cancel_pass_{pass_number}:{exc.code.value}")
            logger.error(
                "Kill switch: cancel-all failed",
                extra={"pass": pass_number, "error_code": exc.code.value},
            )

        try:
            closed = await broker.close_all_positions()
            result.positions_closed += len(closed)
        except BrokerError as exc:
            result.errors.append(f"close_pass_{pass_number}:{exc.code.value}")
            logger.error(
                "Kill switch: close-all failed",
                extra={"pass": pass_number, "error_code": exc.code.value},
            )

        if pass_number == 1:
            await asyncio.sleep(SETTLE_SECONDS)

    if result.errors:
        logger.critical(
            "Kill switch completed WITH ERRORS — the account is locked but may "
            "not be flat. The reconciler will keep trying.",
            extra={"broker_account_id": str(account_id), "errors": result.errors},
        )
    else:
        logger.warning(
            "Kill switch completed cleanly",
            extra={
                "broker_account_id": str(account_id),
                "orders_cancelled": result.orders_cancelled,
                "positions_closed": result.positions_closed,
            },
        )
    return result


async def unlock_account(
    session: AsyncSession, redis: Redis | None, account_id: uuid.UUID
) -> None:
    """Clear the lock. Always explicit and manual — never automatic.

    Nothing in this system unlocks an account on a timer. Whatever caused the
    kill switch to fire deserves a human deciding it is resolved.
    """
    await session.execute(
        update(BrokerAccount)
        .where(BrokerAccount.id == account_id)
        .values(
            trading_state=TradingState.ACTIVE.value, locked_at=None, locked_reason=None
        )
    )
    await session.flush()
    if redis is not None:
        try:
            await redis.delete(f"{_LOCK_KEY}{account_id}")
        except RedisError:
            logger.warning("Could not clear cached lock; Postgres is authoritative")
