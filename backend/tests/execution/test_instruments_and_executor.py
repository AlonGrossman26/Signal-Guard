"""The instrument cache and the decision→order executor (§7, §10, §13).

Both were gaps the audit found: `list_instruments()` existed but nothing ever
wrote the `instruments` table, so every live alert rejected
`INSTRUMENT_UNAVAILABLE`; and no code path turned a decision into an order.

Fake broker throughout — no network (§13).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from signalguard.db.models import BrokerAccount, Instrument
from signalguard.db.models import Order as OrderRow
from signalguard.enums import AlertAction, OrderType, ReasonCode, Verdict
from signalguard.execution.executor import execute_decision
from signalguard.execution.instruments import (
    cache_age_sec,
    refresh_if_stale,
    refresh_instruments,
)
from signalguard.risk.types import AlertInput, Decision
from tests.fakes.fake_broker import FakeBroker

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BROKER = "binance_spot_testnet"


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
            " trading_state) VALUES (:id, :u, :br, :l, "
            " '\\x00'::bytea, '\\x00'::bytea, true, true, 'ACTIVE')"
        ),
        {
            "id": account_id, "u": user_id, "br": BROKER,
            "l": f"acct-{account_id.hex[:8]}",
        },
    )
    await session.flush()
    return (
        await session.execute(
            select(BrokerAccount).where(BrokerAccount.id == account_id)
        )
    ).scalar_one()


async def _seed_decision(session: AsyncSession, account: BrokerAccount) -> uuid.UUID:
    alert_id, decision_id, endpoint_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO webhook_endpoints (id, user_id, endpoint_id_hash, "
            " hmac_secret_encrypted, hmac_secret_nonce, body_secret_encrypted, "
            " body_secret_nonce, is_active) "
            "VALUES (:id, :u, :h, '\\x00'::bytea, '\\x00'::bytea, "
            "        '\\x00'::bytea, '\\x00'::bytea, true)"
        ),
        {"id": endpoint_id, "u": account.user_id, "h": uuid.uuid4().hex},
    )
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
            "VALUES (:id, :a, :b, 'APPROVED', 'APPROVED', '{}'::jsonb, 1, now(), "
            "        1, false)"
        ),
        {"id": decision_id, "a": alert_id, "b": account.id},
    )
    await session.flush()
    return decision_id


def _alert(action: AlertAction = AlertAction.BUY) -> AlertInput:
    return AlertInput(
        symbol="BTCUSDT",
        action=action,
        order_type=OrderType.MARKET,
        timestamp=datetime.now(UTC),
        dedupe_key=uuid.uuid4().hex,
        stop_price=Decimal("58000"),
    )


# --- The instrument cache (P8-4) ---------------------------------------------


async def test_refresh_writes_exchange_filters(session: AsyncSession) -> None:
    """§7: filters come from the exchange and are cached — never hardcoded."""
    now = datetime.now(UTC)
    written = await refresh_instruments(session, FakeBroker(), BROKER, now)

    assert written >= 1
    row = (
        await session.execute(
            select(Instrument).where(
                Instrument.broker == BROKER, Instrument.symbol == "BTCUSDT"
            )
        )
    ).scalar_one()
    assert row.lot_step == Decimal("0.00001")
    assert row.min_notional == Decimal("10")


async def test_refresh_is_an_upsert_not_a_replace(session: AsyncSession) -> None:
    """A symbol that momentarily vanishes must not lose its cached filters."""
    now = datetime.now(UTC)
    await refresh_instruments(session, FakeBroker(), BROKER, now)
    later = now + timedelta(hours=2)
    await refresh_instruments(session, FakeBroker(), BROKER, later)

    rows = (
        await session.execute(
            select(Instrument).where(
                Instrument.broker == BROKER, Instrument.symbol == "BTCUSDT"
            )
        )
    ).scalars().all()
    assert len(rows) == 1, "upsert must not create a second row"
    assert rows[0].fetched_at.replace(microsecond=0) == later.replace(microsecond=0)


async def test_refresh_if_stale_skips_a_fresh_cache(session: AsyncSession) -> None:
    """A 15-second loop must not hit exchangeInfo 5,760 times a day."""
    now = datetime.now(UTC)
    await refresh_instruments(session, FakeBroker(), BROKER, now)

    skipped = await refresh_if_stale(session, FakeBroker(), BROKER, now)
    assert skipped == 0

    stale_moment = now + timedelta(seconds=7200)
    refreshed = await refresh_if_stale(session, FakeBroker(), BROKER, stale_moment)
    assert refreshed >= 1


async def test_cache_age_is_none_when_never_fetched(session: AsyncSession) -> None:
    assert await cache_age_sec(session, "a-broker-we-never-called", datetime.now(UTC)) is None


# --- The executor (P8-2) ------------------------------------------------------


async def test_rejected_decision_submits_nothing(session: AsyncSession) -> None:
    """The decision is the authority. A rejection places no order, ever."""
    account = await _seed_account(session)
    decision_id = await _seed_decision(session, account)
    broker = FakeBroker()

    result = await execute_decision(
        session,
        broker,
        account=account,
        decision=Decision(
            verdict=Verdict.REJECTED,
            reason_code=ReasonCode.SYMBOL_NOT_ALLOWED,
            computed_qty=Decimal("1"),
            rule_snapshot={},
        ),
        decision_id=decision_id,
        alert=_alert(),
    )

    assert result is None
    assert broker.submit_calls == []


async def test_approved_entry_without_a_stop_is_refused(session: AsyncSession) -> None:
    """Engine and executor disagreeing about protection means place nothing."""
    account = await _seed_account(session)
    decision_id = await _seed_decision(session, account)
    broker = FakeBroker()

    result = await execute_decision(
        session,
        broker,
        account=account,
        decision=Decision(
            verdict=Verdict.APPROVED,
            reason_code=ReasonCode.APPROVED,
            computed_qty=Decimal("0.5"),
            stop_price=None,  # the contradiction
            rule_snapshot={},
        ),
        decision_id=decision_id,
        alert=_alert(),
    )

    assert result is None
    assert broker.submit_calls == []


async def test_exit_submits_a_single_market_sell(session: AsyncSession) -> None:
    """An exit closes what is held — one market order, no protective stop."""
    account = await _seed_account(session)
    decision_id = await _seed_decision(session, account)
    broker = FakeBroker()

    result = await execute_decision(
        session,
        broker,
        account=account,
        decision=Decision(
            verdict=Verdict.APPROVED,
            reason_code=ReasonCode.APPROVED,
            computed_qty=Decimal("0.25"),
            rule_snapshot={},
        ),
        decision_id=decision_id,
        alert=_alert(AlertAction.CLOSE),
    )

    assert result is not None
    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0].order_type is OrderType.MARKET

    rows = (
        await session.execute(
            select(OrderRow).where(OrderRow.decision_id == decision_id)
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "EXIT"


async def test_exit_on_a_flat_symbol_submits_nothing(session: AsyncSession) -> None:
    """Approved with zero quantity is legitimate for an exit — and does nothing."""
    account = await _seed_account(session)
    decision_id = await _seed_decision(session, account)
    broker = FakeBroker()

    result = await execute_decision(
        session,
        broker,
        account=account,
        decision=Decision(
            verdict=Verdict.APPROVED,
            reason_code=ReasonCode.APPROVED,
            computed_qty=Decimal("0"),
            rule_snapshot={},
        ),
        decision_id=decision_id,
        alert=_alert(AlertAction.CLOSE),
    )

    assert result is None
    assert broker.submit_calls == []
