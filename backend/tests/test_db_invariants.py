"""Integration tests: the database must enforce its guarantees, not just declare them.

These need a live Postgres with the migration applied, and are skipped when one
is not reachable. Run them with:

    docker compose exec api uv run pytest tests/test_db_invariants.py -q

Everything asserted here is something the application layer could also check —
and that is exactly the point. The application will have bugs. These constraints
are the layer that still holds when it does.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture()
async def session() -> Any:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set — skipping database integration tests")
    engine = create_async_engine(DATABASE_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, is_active) "
            "VALUES (:id, :email, 'argon2-placeholder', true)"
        ),
        {"id": user_id, "email": f"{user_id}@example.test"},
    )
    return user_id


async def _make_account(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO broker_accounts "
            "(id, user_id, broker, label, encrypted_credentials, credentials_nonce, "
            " is_testnet, is_active, trading_state) "
            "VALUES (:id, :user_id, 'binance_spot_testnet', :label, "
            "        '\\x00'::bytea, '\\x00'::bytea, true, true, 'ACTIVE')"
        ),
        {"id": account_id, "user_id": user_id, "label": f"acct-{account_id.hex[:8]}"},
    )
    return account_id


async def _make_endpoint(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    endpoint_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO webhook_endpoints "
            "(id, user_id, endpoint_id_hash, hmac_secret_encrypted, hmac_secret_nonce, "
            " body_secret_encrypted, body_secret_nonce, is_active) "
            "VALUES (:id, :user_id, :hash, '\\x00'::bytea, '\\x00'::bytea, "
            "        '\\x00'::bytea, '\\x00'::bytea, true)"
        ),
        {"id": endpoint_id, "user_id": user_id, "hash": f"hash-{endpoint_id}"},
    )
    return endpoint_id


async def _make_alert(
    session: AsyncSession, user_id: uuid.UUID, endpoint_id: uuid.UUID
) -> uuid.UUID:
    alert_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO alerts "
            "(id, user_id, webhook_endpoint_id, raw_payload, raw_body_sha256, "
            " content_length, received_at, dedupe_key, parse_status, auth_mode, "
            " signature_valid) "
            "VALUES (:id, :user_id, :endpoint_id, '{\"symbol\": \"BTCUSDT\"}'::jsonb, "
            "        '\\x00'::bytea, 42, now(), :dedupe, 'OK', 'HMAC', true)"
        ),
        {
            "id": alert_id,
            "user_id": user_id,
            "endpoint_id": endpoint_id,
            "dedupe": f"dedupe-{alert_id}",
        },
    )
    return alert_id


async def _insert_decision(
    session: AsyncSession, alert_id: uuid.UUID, *, is_test: bool = False
) -> uuid.UUID:
    decision_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO decisions "
            "(id, alert_id, verdict, reason_code, reason_detail, rule_snapshot, "
            " risk_profile_version, evaluated_at, latency_ms, is_test) "
            "VALUES (:id, :alert_id, 'REJECTED', 'NO_STOP_LOSS', NULL, '{}'::jsonb, "
            "        1, now(), 5, :is_test)"
        ),
        {"id": decision_id, "alert_id": alert_id, "is_test": is_test},
    )
    return decision_id


# --- Append-only audit trail --------------------------------------------------


async def test_alerts_cannot_be_updated(session: AsyncSession) -> None:
    """CLAUDE.md §6: alerts are append-only. The trigger must refuse an UPDATE."""
    user_id = await _make_user(session)
    endpoint_id = await _make_endpoint(session, user_id)
    alert_id = await _make_alert(session, user_id, endpoint_id)
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            text("UPDATE alerts SET parse_status = 'OK' WHERE id = :id"),
            {"id": alert_id},
        )
    await session.rollback()


async def test_alerts_cannot_be_deleted(session: AsyncSession) -> None:
    user_id = await _make_user(session)
    endpoint_id = await _make_endpoint(session, user_id)
    alert_id = await _make_alert(session, user_id, endpoint_id)
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            text("DELETE FROM alerts WHERE id = :id"), {"id": alert_id}
        )
    await session.rollback()


async def test_decisions_cannot_be_updated(session: AsyncSession) -> None:
    """A decision that could be edited later is not an audit record."""
    user_id = await _make_user(session)
    endpoint_id = await _make_endpoint(session, user_id)
    alert_id = await _make_alert(session, user_id, endpoint_id)
    await _insert_decision(session, alert_id)
    await session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await session.execute(
            text("UPDATE decisions SET verdict = 'APPROVED' WHERE alert_id = :id"),
            {"id": alert_id},
        )
    await session.rollback()


# --- Idempotency backstop -----------------------------------------------------


async def test_one_live_decision_per_alert(session: AsyncSession) -> None:
    """Two workers racing on one alert cannot both write a decision.

    This is the database-level guarantee behind constraint #4: the same alert
    delivered twice produces exactly one order, because it produces exactly one
    live decision.
    """
    user_id = await _make_user(session)
    endpoint_id = await _make_endpoint(session, user_id)
    alert_id = await _make_alert(session, user_id, endpoint_id)
    await _insert_decision(session, alert_id)
    await session.commit()

    with pytest.raises(IntegrityError):
        await _insert_decision(session, alert_id)
        await session.commit()
    await session.rollback()


async def test_test_decisions_are_exempt_from_the_unique_index(
    session: AsyncSession,
) -> None:
    """/test may be run repeatedly on the same alert — that is its whole purpose."""
    user_id = await _make_user(session)
    endpoint_id = await _make_endpoint(session, user_id)
    alert_id = await _make_alert(session, user_id, endpoint_id)

    await _insert_decision(session, alert_id, is_test=True)
    await _insert_decision(session, alert_id, is_test=True)
    await _insert_decision(session, alert_id, is_test=True)
    await session.commit()  # must not raise


# --- Constraint #2: testnet only ----------------------------------------------


async def test_live_broker_account_is_refused_by_the_database(
    session: AsyncSession,
) -> None:
    """Constraint #2 must hold even against a direct INSERT that bypasses the app."""
    user_id = await _make_user(session)
    await session.commit()

    with pytest.raises(IntegrityError, match="testnet_only"):
        await session.execute(
            text(
                "INSERT INTO broker_accounts "
                "(id, user_id, broker, label, encrypted_credentials, "
                " credentials_nonce, is_testnet, is_active, trading_state) "
                "VALUES (:id, :user_id, 'binance_spot', 'live-account', "
                "        '\\x00'::bytea, '\\x00'::bytea, false, true, 'ACTIVE')"
            ),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        await session.commit()
    await session.rollback()


# --- Risk profile sanity ------------------------------------------------------


async def test_risk_percentage_above_one_is_refused(session: AsyncSession) -> None:
    """Someone typing "50" meaning 50% would size positions 5,000x too large."""
    user_id = await _make_user(session)
    await session.commit()

    with pytest.raises(IntegrityError, match="risk_pct_fraction"):
        await session.execute(
            text(
                "INSERT INTO risk_profiles (id, user_id, risk_per_trade_pct, "
                " max_notional_per_trade, max_total_notional) "
                "VALUES (:id, :user_id, 50, 1000, 5000)"
            ),
            {"id": uuid.uuid4(), "user_id": user_id},
        )
        await session.commit()
    await session.rollback()


async def test_new_risk_profile_allows_no_symbols(session: AsyncSession) -> None:
    """Fail closed by default: a fresh profile trades nothing."""
    user_id = await _make_user(session)
    await session.execute(
        text(
            "INSERT INTO risk_profiles (id, user_id, max_notional_per_trade, "
            " max_total_notional) VALUES (:id, :user_id, 1000, 5000)"
        ),
        {"id": uuid.uuid4(), "user_id": user_id},
    )
    await session.commit()

    result = await session.execute(
        text("SELECT allowed_symbols FROM risk_profiles WHERE user_id = :id"),
        {"id": user_id},
    )
    assert result.scalar_one() == []


# --- Daily baseline uniqueness (the DST / restart guarantee) -------------------


async def test_one_equity_baseline_per_trading_day(session: AsyncSession) -> None:
    """A restart must not be able to mint a second baseline for the same day."""
    user_id = await _make_user(session)
    account_id = await _make_account(session, user_id)
    await session.commit()

    day = date(2026, 3, 29)  # a European DST transition day
    for _ in range(1):
        await session.execute(
            text(
                "INSERT INTO equity_snapshots (id, broker_account_id, equity, "
                " taken_at, is_session_baseline, session_date) "
                "VALUES (:id, :account, 10000, now(), true, :day)"
            ),
            {"id": uuid.uuid4(), "account": account_id, "day": day},
        )
    await session.commit()

    with pytest.raises(IntegrityError):
        await session.execute(
            text(
                "INSERT INTO equity_snapshots (id, broker_account_id, equity, "
                " taken_at, is_session_baseline, session_date) "
                "VALUES (:id, :account, 9500, now(), true, :day)"
            ),
            {"id": uuid.uuid4(), "account": account_id, "day": day},
        )
        await session.commit()
    await session.rollback()


async def test_non_baseline_snapshots_are_unconstrained(session: AsyncSession) -> None:
    """Ordinary equity ticks are frequent — only the baseline is one-per-day."""
    user_id = await _make_user(session)
    account_id = await _make_account(session, user_id)

    now = datetime.now(UTC)
    for i in range(5):
        await session.execute(
            text(
                "INSERT INTO equity_snapshots (id, broker_account_id, equity, "
                " taken_at, is_session_baseline, session_date) "
                "VALUES (:id, :account, 10000, :taken, false, :day)"
            ),
            {
                "id": uuid.uuid4(),
                "account": account_id,
                "taken": now + timedelta(minutes=i),
                "day": date(2026, 3, 29),
            },
        )
    await session.commit()  # must not raise


# --- Constraint #3: exact decimals --------------------------------------------


async def test_decimal_round_trips_without_precision_loss(
    session: AsyncSession,
) -> None:
    """The whole point of NUMERIC(36,18): what goes in is what comes out.

    0.1 + 0.2 in float is 0.30000000000000004. A quantity that drifts like that
    is rejected by the exchange at best, and wrong at worst.
    """
    user_id = await _make_user(session)
    account_id = await _make_account(session, user_id)

    exact = Decimal("0.089285714285714285")
    await session.execute(
        text(
            "INSERT INTO positions (id, broker_account_id, symbol, qty, avg_entry) "
            "VALUES (:id, :account, 'BTCUSDT', :qty, 62000)"
        ),
        {"id": uuid.uuid4(), "account": account_id, "qty": exact},
    )
    await session.commit()

    result = await session.execute(
        text("SELECT qty FROM positions WHERE broker_account_id = :account"),
        {"account": account_id},
    )
    stored = result.scalar_one()
    assert isinstance(stored, Decimal)
    assert stored == exact
    assert str(stored) == "0.089285714285714285"
