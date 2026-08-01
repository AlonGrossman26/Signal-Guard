"""Closed round-trips and the circuit-breaker state they drive (§6, §7, §13).

These cover the two tables that the 2026-08-01 audit found were never written:
`trades` and `circuit_breaker_state`. Without them rule 7 could not fire in
production no matter how many losses a user took, which made the breaker's
otherwise-correct pure logic unreachable.

Real Postgres, no network — the guarantees under test (idempotent trade
building, a breaker that survives a restart) are produced by SQL, so faking the
database would only test the fake.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from signalguard.db.models import BrokerAccount, CircuitBreakerState, Trade
from signalguard.enums import CircuitState, OrderRole, OrderSide, OrderStatus, OrderType
from signalguard.execution.trades import (
    build_trades,
    recompute_circuit_breaker,
    settle_account,
)

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


async def _seed_account(
    session: AsyncSession,
    *,
    threshold: int = 3,
    cooldown_minutes: int = 60,
    manual_reset: bool = False,
) -> BrokerAccount:
    user_id, account_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text("INSERT INTO users (id, email, password_hash) VALUES (:id, :e, 'x')"),
        {"id": user_id, "e": f"{user_id}@example.test"},
    )
    await session.execute(
        text(
            "INSERT INTO risk_profiles (id, user_id, allowed_symbols, "
            " consecutive_loss_threshold, circuit_breaker_cooldown_minutes, "
            " circuit_breaker_manual_reset) "
            "VALUES (:id, :u, ARRAY['BTCUSDT'], :t, :c, :m)"
        ),
        {
            "id": uuid.uuid4(),
            "u": user_id,
            "t": threshold,
            "c": cooldown_minutes,
            "m": manual_reset,
        },
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
    # Alerts require an endpoint; trades hang off decisions which hang off alerts.
    await session.execute(
        text(
            "INSERT INTO webhook_endpoints (id, user_id, endpoint_id_hash, "
            " hmac_secret_encrypted, hmac_secret_nonce, body_secret_encrypted, "
            " body_secret_nonce, is_active) "
            "VALUES (:id, :u, :h, '\\x00'::bytea, '\\x00'::bytea, "
            "        '\\x00'::bytea, '\\x00'::bytea, true)"
        ),
        {"id": uuid.uuid4(), "u": user_id, "h": uuid.uuid4().hex},
    )
    await session.flush()
    return (
        await session.execute(
            select(BrokerAccount).where(BrokerAccount.id == account_id)
        )
    ).scalar_one()


async def _endpoint_id(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    row = await session.execute(
        text("SELECT id FROM webhook_endpoints WHERE user_id = :u LIMIT 1"),
        {"u": user_id},
    )
    return row.scalar_one()  # type: ignore[no-any-return]


async def _round_trip(
    session: AsyncSession,
    account: BrokerAccount,
    *,
    entry_price: str,
    exit_price: str,
    qty: str = "1",
    fees: str = "0",
    closed_at: datetime | None = None,
) -> None:
    """Insert a filled ENTRY and a filled STOP sharing one decision."""
    alert_id, decision_id = uuid.uuid4(), uuid.uuid4()
    endpoint_id = await _endpoint_id(session, account.user_id)
    await session.execute(
        text(
            "INSERT INTO alerts (id, user_id, webhook_endpoint_id, raw_payload, "
            " raw_body_sha256, content_length, received_at, dedupe_key, "
            " parse_status, auth_mode, signature_valid) "
            "VALUES (:id, :u, :e, '{}'::jsonb, '\\x00'::bytea, 0, now(), :k, "
            "        'OK', 'HMAC', true)"
        ),
        {"id": alert_id, "u": account.user_id, "e": endpoint_id, "k": uuid.uuid4().hex},
    )
    await session.execute(
        text(
            "INSERT INTO decisions (id, alert_id, broker_account_id, verdict, "
            " reason_code, rule_snapshot, risk_profile_version, evaluated_at, "
            " latency_ms, is_test) "
            "VALUES (:id, :a, :b, 'APPROVED', 'APPROVED', '{}'::jsonb, 1, now(), 1, false)"
        ),
        {"id": decision_id, "a": alert_id, "b": account.id},
    )

    when = closed_at or datetime.now(UTC)
    for role, side, price in (
        (OrderRole.ENTRY, OrderSide.BUY, entry_price),
        (OrderRole.STOP, OrderSide.SELL, exit_price),
    ):
        await session.execute(
            text(
                "INSERT INTO orders (id, decision_id, broker_account_id, "
                " client_order_id, symbol, side, type, role, qty, status, "
                " filled_qty, avg_fill_price, fees, filled_at) "
                "VALUES (:id, :d, :b, :c, 'BTCUSDT', :s, :t, :r, :q, :st, :q, :p, "
                "        :f, :w)"
            ),
            {
                "id": uuid.uuid4(),
                "d": decision_id,
                "b": account.id,
                "c": f"sg{uuid.uuid4().hex[:30]}",
                "s": side.value,
                "t": OrderType.MARKET.value,
                "r": role.value,
                "q": Decimal(qty),
                "st": OrderStatus.FILLED.value,
                "p": Decimal(price),
                # Split the fee across both legs so the total is `fees`.
                "f": Decimal(fees) / 2,
                "w": when,
            },
        )
    await session.flush()


# --- Building trades ----------------------------------------------------------


async def test_a_filled_round_trip_becomes_one_trade(session: AsyncSession) -> None:
    account = await _seed_account(session)
    await _round_trip(session, account, entry_price="100", exit_price="110", qty="2")

    trades = await build_trades(session, account, datetime.now(UTC))

    assert len(trades) == 1
    assert trades[0].realized_pnl == Decimal("20")  # (110 - 100) x 2
    assert trades[0].symbol == "BTCUSDT"


async def test_fees_are_subtracted_from_realized_pnl(session: AsyncSession) -> None:
    """A gross-positive, fee-negative trade is a loss — the breaker must see one."""
    account = await _seed_account(session)
    await _round_trip(
        session, account, entry_price="100", exit_price="101", qty="1", fees="4"
    )

    trades = await build_trades(session, account, datetime.now(UTC))

    assert len(trades) == 1
    assert trades[0].realized_pnl == Decimal("-3")  # +1 gross, 4 in fees
    assert trades[0].realized_pnl < 0


async def test_building_twice_does_not_duplicate(session: AsyncSession) -> None:
    """Runs on every reconciliation cycle, so this has to be idempotent."""
    account = await _seed_account(session)
    await _round_trip(session, account, entry_price="100", exit_price="90")

    first = await build_trades(session, account, datetime.now(UTC))
    second = await build_trades(session, account, datetime.now(UTC))

    assert len(first) == 1
    assert second == []
    total = (
        await session.execute(
            select(Trade).where(Trade.broker_account_id == account.id)
        )
    ).scalars().all()
    assert len(total) == 1


# --- The circuit breaker ------------------------------------------------------


async def test_breaker_stays_closed_below_the_threshold(session: AsyncSession) -> None:
    account = await _seed_account(session, threshold=3)
    now = datetime.now(UTC)
    for i in range(2):
        await _round_trip(
            session, account, entry_price="100", exit_price="90",
            closed_at=now - timedelta(minutes=10 - i),
        )
    await build_trades(session, account, now)

    update = await recompute_circuit_breaker(session, account, now)

    assert update is not None
    assert update.consecutive_losses == 2
    assert update.state is CircuitState.CLOSED
    assert update.newly_opened is False


async def test_breaker_opens_exactly_at_the_threshold(session: AsyncSession) -> None:
    """The boundary case §13 asks for: exactly at the threshold, not one past it."""
    account = await _seed_account(session, threshold=3)
    now = datetime.now(UTC)
    for i in range(3):
        await _round_trip(
            session, account, entry_price="100", exit_price="90",
            closed_at=now - timedelta(minutes=10 - i),
        )
    await build_trades(session, account, now)

    update = await recompute_circuit_breaker(session, account, now)

    assert update is not None
    assert update.consecutive_losses == 3
    assert update.state is CircuitState.OPEN
    assert update.newly_opened is True
    assert update.cooldown_until is not None


async def test_a_win_resets_the_streak(session: AsyncSession) -> None:
    account = await _seed_account(session, threshold=3)
    now = datetime.now(UTC)
    for i in range(3):
        await _round_trip(
            session, account, entry_price="100", exit_price="90",
            closed_at=now - timedelta(minutes=20 - i),
        )
    # ...then a win, most recently.
    await _round_trip(
        session, account, entry_price="100", exit_price="120",
        closed_at=now - timedelta(minutes=1),
    )
    await build_trades(session, account, now)

    update = await recompute_circuit_breaker(session, account, now)

    assert update is not None
    assert update.consecutive_losses == 0
    assert update.state is CircuitState.CLOSED


async def test_breaker_state_survives_a_restart(session: AsyncSession) -> None:
    """§7: the state lives in Postgres and must outlive the process.

    Simulated the only way that is meaningful in-process: recompute from a clean
    read of the durable table, exactly as a freshly-booted worker would.
    """
    account = await _seed_account(session, threshold=3)
    now = datetime.now(UTC)
    for i in range(3):
        await _round_trip(
            session, account, entry_price="100", exit_price="90",
            closed_at=now - timedelta(minutes=10 - i),
        )
    await settle_account(session, account, now)
    await session.commit()

    row = (
        await session.execute(
            select(CircuitBreakerState).where(
                CircuitBreakerState.broker_account_id == account.id
            )
        )
    ).scalar_one()
    assert row.state == CircuitState.OPEN.value
    assert row.consecutive_losses == 3

    # A second pass (the "restarted" worker) finds it already open and does not
    # re-arm the cooldown — otherwise the cooldown would never expire.
    original_cooldown = row.cooldown_until
    later = now + timedelta(minutes=5)
    update = await recompute_circuit_breaker(session, account, later)

    assert update is not None
    assert update.state is CircuitState.OPEN
    assert update.newly_opened is False, "already-open must not re-alert"
    assert update.cooldown_until == original_cooldown


async def test_manual_reset_profile_records_no_cooldown(session: AsyncSession) -> None:
    """A user who asked to be stopped stays stopped until they say otherwise."""
    account = await _seed_account(session, threshold=2, manual_reset=True)
    now = datetime.now(UTC)
    for i in range(2):
        await _round_trip(
            session, account, entry_price="100", exit_price="90",
            closed_at=now - timedelta(minutes=10 - i),
        )
    await build_trades(session, account, now)

    update = await recompute_circuit_breaker(session, account, now)

    assert update is not None
    assert update.state is CircuitState.OPEN
    assert update.cooldown_until is None
