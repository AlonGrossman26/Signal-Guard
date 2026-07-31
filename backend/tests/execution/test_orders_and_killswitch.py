"""Execution: naked-position prevention, kill switch, reconciliation.

Every test here runs against the fake broker (no network, per CLAUDE.md §13),
but through the real execution code. The scenarios are the ones that cost money
when they go wrong.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from signalguard.enums import OrderRole, OrderSide, OrderStatus, OrderType, TradingState
from signalguard.execution.base import BrokerErrorCode, BrokerPosition
from signalguard.execution.killswitch import (
    fire_kill_switch,
    is_locked,
    set_locked,
    unlock_account,
)
from signalguard.execution.orders import new_client_order_id, submit_entry_with_stop
from tests.fakes.fake_broker import FakeBroker, FakeBrokerBehaviour

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


async def _seed_account(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a user, account, alert and decision. Returns (account_id, decision_id)."""
    user_id, account_id = uuid.uuid4(), uuid.uuid4()
    endpoint_id, alert_id, decision_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

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
    await session.execute(
        text(
            "INSERT INTO webhook_endpoints (id, user_id, endpoint_id_hash, "
            " hmac_secret_encrypted, hmac_secret_nonce, body_secret_encrypted, "
            " body_secret_nonce) VALUES (:id, :u, :h, '\\x00'::bytea, "
            " '\\x00'::bytea, '\\x00'::bytea, '\\x00'::bytea)"
        ),
        {"id": endpoint_id, "u": user_id, "h": f"h-{endpoint_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO alerts (id, user_id, webhook_endpoint_id, raw_payload, "
            " raw_body_sha256, content_length, received_at, dedupe_key, parse_status, "
            " auth_mode, signature_valid) VALUES (:id, :u, :ep, '{}'::jsonb, "
            " '\\x00'::bytea, 10, now(), :d, 'OK', 'HMAC', true)"
        ),
        {"id": alert_id, "u": user_id, "ep": endpoint_id, "d": f"d-{alert_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO decisions (id, alert_id, broker_account_id, verdict, "
            " reason_code, rule_snapshot, risk_profile_version, evaluated_at, "
            " latency_ms) VALUES (:id, :a, :acc, 'APPROVED', 'APPROVED', '{}'::jsonb, "
            " 1, now(), 5)"
        ),
        {"id": decision_id, "a": alert_id, "acc": account_id},
    )
    await session.commit()
    return account_id, decision_id


async def _orders_for(session: AsyncSession, decision_id: uuid.UUID) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT role, status, qty, client_order_id, last_error FROM orders "
            "WHERE decision_id = :d ORDER BY role"
        ),
        {"d": decision_id},
    )
    return [dict(r._mapping) for r in result]


# --- Never leave a naked position ---------------------------------------------


async def test_happy_path_places_entry_and_stop(session: AsyncSession) -> None:
    account_id, decision_id = await _seed_account(session)
    broker = FakeBroker()

    result = await submit_entry_with_stop(
        session, broker,
        decision_id=decision_id, broker_account_id=account_id,
        symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=Decimal("0.05"), limit_price=None, stop_price=Decimal("61000"),
    )
    await session.commit()

    assert result.is_protected
    rows = await _orders_for(session, decision_id)
    assert {r["role"] for r in rows} == {OrderRole.ENTRY.value, OrderRole.STOP.value}


async def test_stop_failure_closes_the_naked_position(session: AsyncSession) -> None:
    """The central guarantee: if the stop cannot be placed, the position goes.

    A worse trading outcome (an unwanted round-trip and its fees) in exchange for
    a bounded risk outcome. This project makes that trade every time.
    """
    account_id, decision_id = await _seed_account(session)
    broker = FakeBroker(behaviour=FakeBrokerBehaviour(fail_stop_submit=True))

    result = await submit_entry_with_stop(
        session, broker,
        decision_id=decision_id, broker_account_id=account_id,
        symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=Decimal("0.05"), limit_price=None, stop_price=Decimal("61000"),
    )
    await session.commit()

    assert not result.is_protected
    assert result.naked_position_closed is True
    # The position was opened and then closed — the broker holds nothing.
    assert "BTCUSDT" not in broker.positions

    rows = await _orders_for(session, decision_id)
    roles = {r["role"] for r in rows}
    assert OrderRole.EXIT.value in roles, "an emergency close order must be recorded"


async def test_entry_rejection_leaves_nothing_behind(session: AsyncSession) -> None:
    """No position was opened, so there is nothing to unwind."""
    account_id, decision_id = await _seed_account(session)
    broker = FakeBroker(
        behaviour=FakeBrokerBehaviour(
            fail_next_submit=BrokerErrorCode.INSUFFICIENT_BALANCE
        )
    )

    result = await submit_entry_with_stop(
        session, broker,
        decision_id=decision_id, broker_account_id=account_id,
        symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=Decimal("0.05"), limit_price=None, stop_price=Decimal("61000"),
    )
    await session.commit()

    assert result.entry is None
    assert not broker.positions
    rows = await _orders_for(session, decision_id)
    assert rows[0]["status"] == OrderStatus.REJECTED.value
    assert rows[0]["last_error"] == BrokerErrorCode.INSUFFICIENT_BALANCE.value


async def test_partial_fill_stop_covers_only_what_filled(
    session: AsyncSession,
) -> None:
    """A stop for the requested size would try to sell more than we own."""
    account_id, decision_id = await _seed_account(session)
    broker = FakeBroker(
        behaviour=FakeBrokerBehaviour(partial_fill_ratio=Decimal("0.4"))
    )

    result = await submit_entry_with_stop(
        session, broker,
        decision_id=decision_id, broker_account_id=account_id,
        symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=Decimal("0.05"), limit_price=None, stop_price=Decimal("61000"),
    )
    await session.commit()

    assert result.is_protected
    stop_request = next(r for r in broker.submit_calls if r.stop_price is not None)
    assert stop_request.qty == Decimal("0.05") * Decimal("0.4")


async def test_timeout_on_entry_is_recorded_not_silently_retried(
    session: AsyncSession,
) -> None:
    """A timeout leaves the outcome unknown. Retrying blindly could double-submit."""
    account_id, decision_id = await _seed_account(session)
    broker = FakeBroker(behaviour=FakeBrokerBehaviour(timeout_on_submit=True))

    result = await submit_entry_with_stop(
        session, broker,
        decision_id=decision_id, broker_account_id=account_id,
        symbol="BTCUSDT", side=OrderSide.BUY, order_type=OrderType.MARKET,
        qty=Decimal("0.05"), limit_price=None, stop_price=Decimal("61000"),
    )
    await session.commit()

    assert result.entry is None
    assert len(broker.submit_calls) == 1, "must not blindly retry a submit"
    rows = await _orders_for(session, decision_id)
    assert rows[0]["last_error"] == BrokerErrorCode.TIMEOUT.value
    # The client_order_id is persisted, so reconciliation can ask what happened.
    assert rows[0]["client_order_id"].startswith("sg")


async def test_same_client_order_id_never_becomes_two_orders() -> None:
    """Constraint #4 at the broker boundary: a retry is the same order."""
    broker = FakeBroker()
    from signalguard.execution.base import OrderRequest

    request = OrderRequest(
        client_order_id=new_client_order_id(),
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal("0.05"),
    )
    first = await broker.submit_order(request)
    second = await broker.submit_order(request)

    assert first.broker_order_id == second.broker_order_id
    assert len(broker.orders) == 1


# --- Kill switch --------------------------------------------------------------


async def test_kill_switch_locks_then_flattens(session: AsyncSession) -> None:
    account_id, _ = await _seed_account(session)
    broker = FakeBroker(
        positions={
            "BTCUSDT": BrokerPosition(
                symbol="BTCUSDT", qty=Decimal("0.1"),
                avg_entry=Decimal("60000"), mark_price=Decimal("62000"),
            )
        }
    )

    result = await fire_kill_switch(session, None, broker, account_id, "test")
    await session.commit()

    assert result.locked
    assert await is_locked(session, None, account_id)
    assert not broker.positions


async def test_kill_switch_sweeps_twice(session: AsyncSession) -> None:
    """An order landing during the first sweep is caught by the second.

    The in-flight race (plan §3, Q1) is not won — it is outlasted.
    """
    account_id, _ = await _seed_account(session)
    broker = FakeBroker()

    await fire_kill_switch(session, None, broker, account_id, "test")
    await session.commit()

    assert broker.cancel_all_calls == 2
    assert broker.close_all_calls == 2


async def test_kill_switch_is_idempotent(session: AsyncSession) -> None:
    """Firing twice is harmless — it is a panic button, it will get double-pressed."""
    account_id, _ = await _seed_account(session)
    broker = FakeBroker()

    first = await fire_kill_switch(session, None, broker, account_id, "test")
    second = await fire_kill_switch(session, None, broker, account_id, "test again")
    await session.commit()

    assert first.locked and second.locked
    assert await is_locked(session, None, account_id)


async def test_kill_switch_locks_even_when_the_broker_fails(
    session: AsyncSession,
) -> None:
    """The lock must hold even if flattening does not.

    A locked-but-not-flat account is bad; an unlocked-and-not-flat one is worse,
    because it can still take on new risk. The reconciler keeps retrying the
    flatten.
    """
    account_id, _ = await _seed_account(session)
    broker = FakeBroker(
        behaviour=FakeBrokerBehaviour(fail_cancel_all=True, fail_close_all=True)
    )

    result = await fire_kill_switch(session, None, broker, account_id, "test")
    await session.commit()

    assert result.locked is True
    assert result.errors
    assert await is_locked(session, None, account_id)


async def test_unlock_is_explicit(session: AsyncSession) -> None:
    """Nothing unlocks an account on a timer."""
    account_id, _ = await _seed_account(session)
    await set_locked(session, None, account_id, "test")
    await session.commit()
    assert await is_locked(session, None, account_id)

    await unlock_account(session, None, account_id)
    await session.commit()
    assert not await is_locked(session, None, account_id)


async def test_unknown_account_reads_as_locked(session: AsyncSession) -> None:
    """Fail closed: an account we cannot confirm is not a tradeable account."""
    assert await is_locked(session, None, uuid.uuid4())


# --- Reconciliation -----------------------------------------------------------


async def test_reconciler_repairs_a_lost_submission(session: AsyncSession) -> None:
    """The order that filled while we thought it had failed.

    A PENDING_SUBMIT row whose response was lost is exactly the state that makes
    reconciliation necessary — and the client_order_id is what makes recovery
    possible.
    """
    from sqlalchemy import select

    from signalguard.db.models import BrokerAccount
    from signalguard.execution.reconciler import reconcile_account

    account_id, decision_id = await _seed_account(session)
    client_order_id = new_client_order_id()

    await session.execute(
        text(
            "INSERT INTO orders (id, decision_id, broker_account_id, client_order_id, "
            " symbol, side, type, role, qty, status) VALUES (:id, :d, :acc, :coid, "
            " 'BTCUSDT', 'BUY', 'MARKET', 'ENTRY', 0.05, 'PENDING_SUBMIT')"
        ),
        {"id": uuid.uuid4(), "d": decision_id, "acc": account_id, "coid": client_order_id},
    )
    await session.commit()

    # The broker says it filled all along.
    broker = FakeBroker()
    from signalguard.execution.base import OrderRequest

    await broker.submit_order(
        OrderRequest(
            client_order_id=client_order_id, symbol="BTCUSDT", side=OrderSide.BUY,
            order_type=OrderType.MARKET, qty=Decimal("0.05"),
        )
    )

    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.id == account_id))
    ).scalar_one()
    report = await reconcile_account(session, broker, account, datetime.now(UTC))
    await session.commit()

    assert report.orders_repaired >= 1
    status = (
        await session.execute(
            text("SELECT status FROM orders WHERE client_order_id = :c"),
            {"c": client_order_id},
        )
    ).scalar_one()
    assert status == OrderStatus.FILLED.value


async def test_reconciler_marks_orders_the_broker_never_saw_as_rejected(
    session: AsyncSession,
) -> None:
    """If the exchange never heard of it, the submission never landed."""
    from sqlalchemy import select

    from signalguard.db.models import BrokerAccount
    from signalguard.execution.reconciler import reconcile_account

    account_id, decision_id = await _seed_account(session)
    # Unique per run: this database persists between test runs, so a fixed id
    # would accumulate rows and make the assertion ambiguous.
    lost_id = f"sg-lost-{uuid.uuid4().hex[:12]}"
    await session.execute(
        text(
            "INSERT INTO orders (id, decision_id, broker_account_id, client_order_id, "
            " symbol, side, type, role, qty, status) VALUES (:id, :d, :acc, :coid, "
            " 'BTCUSDT', 'BUY', 'MARKET', 'ENTRY', 0.05, 'PENDING_SUBMIT')"
        ),
        {"id": uuid.uuid4(), "d": decision_id, "acc": account_id, "coid": lost_id},
    )
    await session.commit()

    broker = FakeBroker(behaviour=FakeBrokerBehaviour(unknown_order_on_get=True))
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.id == account_id))
    ).scalar_one()
    await reconcile_account(session, broker, account, datetime.now(UTC))
    await session.commit()

    status = (
        await session.execute(
            text("SELECT status FROM orders WHERE client_order_id = :c"),
            {"c": lost_id},
        )
    ).scalar_one()
    assert status == OrderStatus.REJECTED.value


async def test_reconciler_flattens_a_locked_account(session: AsyncSession) -> None:
    """LOCKED is a continuously-enforced state, not a one-time sweep (plan §3, Q1).

    This is what closes the in-flight-order race: whatever lands on a locked
    account gets flattened on the next cycle.
    """
    from sqlalchemy import select

    from signalguard.db.models import BrokerAccount
    from signalguard.execution.reconciler import reconcile_account

    account_id, _ = await _seed_account(session)
    await set_locked(session, None, account_id, "kill switch")
    await session.commit()

    # A position appears anyway — the in-flight order that landed after the sweep.
    broker = FakeBroker(
        positions={
            "BTCUSDT": BrokerPosition(
                symbol="BTCUSDT", qty=Decimal("0.1"),
                avg_entry=Decimal("60000"), mark_price=Decimal("62000"),
            )
        }
    )
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.id == account_id))
    ).scalar_one()
    assert account.trading_state == TradingState.LOCKED.value

    report = await reconcile_account(session, broker, account, datetime.now(UTC))
    await session.commit()

    assert report.lock_violations_flattened >= 1
    assert not broker.positions
