"""Cache the exchange's trading filters (CLAUDE.md §7).

§7 is explicit: `lot_step`, `min_qty` and `min_notional` are **fetched from the
exchange and cached — never hardcoded**. Sizing divides by these numbers, so a
wrong one is not a cosmetic error: too small a `lot_step` produces an order the
exchange rejects, and a stale `min_notional` produces one it accepts at a size
the user never authorised.

The cache lives in Postgres rather than Redis on purpose (see the `Instrument`
model): a restart while the exchange is unreachable must still be able to size,
and `fetched_at` is what lets the read path refuse filters that have gone stale
past the OQ-5 limit instead of trusting them forever.

Refreshing is driven from the reconciliation cycle rather than only at startup.
Startup-only would mean a process that has been up for a week is sizing against
week-old filters, which is exactly the case `fetched_at` exists to catch.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard.db.models import Instrument as InstrumentRow
from signalguard.execution.base import BrokerAdapter, BrokerError

logger = logging.getLogger(__name__)

# How old the cache may get before we re-fetch. Comfortably inside the 24h
# staleness cap the read path enforces (OQ-5), so a single failed refresh never
# takes the system down — it just means the next cycle tries again.
REFRESH_INTERVAL_SEC = 3600


async def cache_age_sec(
    session: AsyncSession, broker: str, now: datetime
) -> float | None:
    """Seconds since this broker's filters were last fetched, or None if never."""
    newest = await session.execute(
        select(InstrumentRow.fetched_at)
        .where(InstrumentRow.broker == broker)
        .order_by(InstrumentRow.fetched_at.desc())
        .limit(1)
    )
    fetched_at = newest.scalar_one_or_none()
    if fetched_at is None:
        return None
    return (now - fetched_at).total_seconds()


async def refresh_instruments(
    session: AsyncSession,
    adapter: BrokerAdapter,
    broker: str,
    now: datetime,
) -> int:
    """Pull every instrument from the exchange and upsert it. Returns the count.

    Upsert rather than delete-and-insert: a symbol that momentarily disappears
    from `exchangeInfo` should not take its cached filters with it, because the
    read path would then reject every alert for that symbol. Filters going stale
    is a condition we detect; filters vanishing is one we would rather not create.
    """
    try:
        instruments = await adapter.list_instruments()
    except BrokerError as exc:
        # Never fatal. The existing cache stays valid until it ages out, which is
        # the whole reason it is durable.
        logger.warning(
            "Could not refresh instrument filters; keeping the cached set",
            extra={"broker": broker, "error_code": exc.code.value},
        )
        return 0

    if not instruments:
        logger.warning("Exchange returned no instruments", extra={"broker": broker})
        return 0

    rows = [
        {
            "broker": broker,
            "symbol": item.symbol,
            "base_asset": item.base_asset,
            "quote_asset": item.quote_asset,
            "tick_size": item.tick_size,
            "lot_step": item.lot_step,
            "min_qty": item.min_qty,
            "min_notional": item.min_notional,
            "status": item.status,
            "fetched_at": now,
        }
        for item in instruments
    ]

    statement = pg_insert(InstrumentRow).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["broker", "symbol"],
            set_={
                "base_asset": statement.excluded.base_asset,
                "quote_asset": statement.excluded.quote_asset,
                "tick_size": statement.excluded.tick_size,
                "lot_step": statement.excluded.lot_step,
                "min_qty": statement.excluded.min_qty,
                "min_notional": statement.excluded.min_notional,
                "status": statement.excluded.status,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )
    await session.flush()

    logger.info(
        "Instrument filters refreshed",
        extra={"broker": broker, "count": len(rows)},
    )
    return len(rows)


async def refresh_if_stale(
    session: AsyncSession,
    adapter: BrokerAdapter,
    broker: str,
    now: datetime,
    *,
    interval_sec: int = REFRESH_INTERVAL_SEC,
) -> int:
    """Refresh only when the cache is missing or older than `interval_sec`.

    Called on every reconciliation cycle, so this guard is what keeps a 15-second
    loop from hitting `exchangeInfo` 5,760 times a day.
    """
    age = await cache_age_sec(session, broker, now)
    if age is not None and age < interval_sec:
        return 0
    return await refresh_instruments(session, adapter, broker, now)
