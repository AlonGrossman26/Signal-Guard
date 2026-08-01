"""The reconciler's optional dashboard fan-out.

When a sink is supplied, reconciled positions and equity are emitted for the live
feed; when it is not, reconciliation is unchanged. Runs against the fake broker
and a real session — no network (§13).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from signalguard.db.models import BrokerAccount
from signalguard.execution.base import BrokerPosition
from signalguard.execution.reconciler import reconcile_account
from signalguard.realtime import EventType
from tests.fakes.fake_broker import FakeBroker

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture()
async def session():  # type: ignore[no-untyped-def]
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _seed_account(session: AsyncSession) -> BrokerAccount:
    user_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
        {"id": user_id, "e": f"{user_id}@example.test"},
    )
    await session.execute(
        text(
            "INSERT INTO broker_accounts (id, user_id, broker, label, "
            " encrypted_credentials, credentials_nonce, is_testnet, is_active, "
            " trading_state) VALUES (:id, :u, 'binance_spot_testnet', :l, "
            " '\\x00'::bytea, '\\x00'::bytea, true, true, 'ACTIVE')"
        ),
        {"id": account_id, "u": user_id, "l": f"acct-{account_id.hex[:8]}"},
    )
    await session.flush()
    return (
        await session.execute(
            select(BrokerAccount).where(BrokerAccount.id == account_id)
        )
    ).scalar_one()


async def test_reconcile_emits_position_and_equity_events(session: AsyncSession) -> None:
    account = await _seed_account(session)
    broker = FakeBroker(
        positions={
            "BTCUSDT": BrokerPosition(
                symbol="BTCUSDT",
                qty=Decimal("0.01"),
                avg_entry=Decimal("60000"),
                mark_price=Decimal("62000"),
            )
        }
    )

    events: list[tuple[EventType, dict[str, Any]]] = []

    async def sink(event_type: EventType, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    await reconcile_account(
        session, broker, account, datetime.now(UTC), event_sink=sink
    )

    kinds = [e[0] for e in events]
    assert EventType.POSITION in kinds
    assert EventType.EQUITY in kinds

    position_event = next(d for t, d in events if t is EventType.POSITION)
    assert position_event["symbol"] == "BTCUSDT"
    # Unrealized PnL = (62000 - 60000) * 0.01 = 20.
    assert position_event["unrealized_pnl"] == Decimal("20.00")

    equity_event = next(d for t, d in events if t is EventType.EQUITY)
    assert equity_event["equity"] == broker.free_balance + Decimal("620")


async def test_reconcile_without_sink_emits_nothing(session: AsyncSession) -> None:
    account = await _seed_account(session)
    broker = FakeBroker(
        positions={
            "BTCUSDT": BrokerPosition(
                symbol="BTCUSDT", qty=Decimal("0.01"),
                avg_entry=Decimal("60000"), mark_price=Decimal("62000"),
            )
        }
    )
    # No sink: reconciliation still succeeds and repairs state, just silently.
    report = await reconcile_account(session, broker, account, datetime.now(UTC))
    assert report.positions_synced == 1
