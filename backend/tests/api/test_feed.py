"""Read models: decisions (with filtering), orders, positions, equity curve.

These endpoints have no write path of their own — decisions and orders are
produced by the ingress and execution layers — so the fixtures seed rows
directly, then assert the API reads them back scoped to the right user.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient

from signalguard.db.models import (
    Alert,
    Decision,
    EquitySnapshot,
    Order,
    Position,
)
from tests.api.conftest import db_sessionmaker, register

pytestmark = pytest.mark.integration


async def _bootstrap(client: AsyncClient) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Register a user with an account and endpoint; return their IDs."""
    await register(client)
    user_id = uuid.UUID((await client.get("/api/auth/me")).json()["id"])
    account_id = uuid.UUID(
        (
            await client.post(
                "/api/broker-accounts",
                json={"label": "acct", "api_key": "k", "api_secret": "s"},
            )
        ).json()["id"]
    )
    endpoint_id = uuid.UUID(
        (await client.post("/api/webhook-endpoints")).json()["id"]
    )
    return user_id, account_id, endpoint_id


def _alert(user_id: uuid.UUID, endpoint_id: uuid.UUID) -> Alert:
    return Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        webhook_endpoint_id=endpoint_id,
        raw_payload={"symbol": "BTCUSDT"},
        raw_body_sha256=b"\x00" * 32,
        content_length=10,
        received_at=datetime.now(UTC),
        dedupe_key=uuid.uuid4().hex,
        parse_status="OK",
        auth_mode="HMAC",
        signature_valid=True,
    )


def _decision(
    alert_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    verdict: str,
    reason_code: str,
    when: datetime,
    is_test: bool = False,
) -> Decision:
    return Decision(
        id=uuid.uuid4(),
        alert_id=alert_id,
        broker_account_id=account_id,
        verdict=verdict,
        reason_code=reason_code,
        reason_detail=None,
        rule_snapshot={},
        risk_profile_version=1,
        computed_qty=Decimal("0.001") if verdict == "APPROVED" else None,
        entry_reference_price=Decimal("62000") if verdict == "APPROVED" else None,
        stop_price=Decimal("61000") if verdict == "APPROVED" else None,
        evaluated_at=when,
        latency_ms=5,
        is_test=is_test,
    )


async def test_decisions_feed_newest_first_and_filtered(client: AsyncClient) -> None:
    user_id, account_id, endpoint_id = await _bootstrap(client)
    now = datetime.now(UTC)

    maker = db_sessionmaker()
    async with maker() as s:
        older = _alert(user_id, endpoint_id)
        newer = _alert(user_id, endpoint_id)
        s.add_all([older, newer])
        await s.flush()
        s.add_all(
            [
                _decision(
                    older.id, account_id, verdict="REJECTED",
                    reason_code="SYMBOL_NOT_ALLOWED", when=now - timedelta(minutes=5),
                ),
                _decision(
                    newer.id, account_id, verdict="APPROVED",
                    reason_code="APPROVED", when=now,
                ),
            ]
        )
        await s.commit()

    feed = (await client.get("/api/decisions")).json()
    assert len(feed) == 2
    # Newest first.
    assert feed[0]["reason_code"] == "APPROVED"
    assert feed[0]["computed_qty"] == "0.001000000000000000"

    # Filter by reason code.
    rejected = (
        await client.get("/api/decisions", params={"reason_code": "SYMBOL_NOT_ALLOWED"})
    ).json()
    assert len(rejected) == 1
    assert rejected[0]["verdict"] == "REJECTED"


async def test_decisions_reject_unknown_reason_code(client: AsyncClient) -> None:
    await _bootstrap(client)
    response = await client.get("/api/decisions", params={"reason_code": "NONSENSE"})
    assert response.status_code == 422


async def test_test_decisions_excluded_by_default(client: AsyncClient) -> None:
    user_id, account_id, endpoint_id = await _bootstrap(client)
    maker = db_sessionmaker()
    async with maker() as s:
        alert = _alert(user_id, endpoint_id)
        s.add(alert)
        await s.flush()
        s.add(
            _decision(
                alert.id, account_id, verdict="REJECTED",
                reason_code="NO_STOP_LOSS", when=datetime.now(UTC), is_test=True,
            )
        )
        await s.commit()

    assert (await client.get("/api/decisions")).json() == []
    with_tests = (
        await client.get("/api/decisions", params={"include_tests": True})
    ).json()
    assert len(with_tests) == 1


async def test_decisions_pagination(client: AsyncClient) -> None:
    user_id, account_id, endpoint_id = await _bootstrap(client)
    now = datetime.now(UTC)
    maker = db_sessionmaker()
    async with maker() as s:
        for i in range(5):
            alert = _alert(user_id, endpoint_id)
            s.add(alert)
            await s.flush()
            s.add(
                _decision(
                    alert.id, account_id, verdict="APPROVED", reason_code="APPROVED",
                    when=now - timedelta(minutes=i),
                )
            )
        await s.commit()

    page = (await client.get("/api/decisions", params={"limit": 2})).json()
    assert len(page) == 2
    page2 = (
        await client.get("/api/decisions", params={"limit": 2, "offset": 2})
    ).json()
    assert len(page2) == 2
    assert page[0]["id"] != page2[0]["id"]


async def test_feed_is_scoped_per_user(
    client: AsyncClient, make_client: object
) -> None:
    user_id, account_id, endpoint_id = await _bootstrap(client)
    maker = db_sessionmaker()
    async with maker() as s:
        alert = _alert(user_id, endpoint_id)
        s.add(alert)
        await s.flush()
        s.add(
            _decision(
                alert.id, account_id, verdict="APPROVED", reason_code="APPROVED",
                when=datetime.now(UTC),
            )
        )
        await s.commit()

    async with make_client() as other:  # type: ignore[operator]
        await register(other)
        assert (await other.get("/api/decisions")).json() == []


async def test_orders_positions_equity(client: AsyncClient) -> None:
    user_id, account_id, endpoint_id = await _bootstrap(client)
    now = datetime.now(UTC)
    maker = db_sessionmaker()
    async with maker() as s:
        alert = _alert(user_id, endpoint_id)
        s.add(alert)
        await s.flush()
        decision = _decision(
            alert.id, account_id, verdict="APPROVED", reason_code="APPROVED", when=now
        )
        s.add(decision)
        await s.flush()
        s.add(
            Order(
                id=uuid.uuid4(),
                decision_id=decision.id,
                broker_account_id=account_id,
                client_order_id="cid-1",
                symbol="BTCUSDT",
                side="BUY",
                type="LIMIT",
                role="ENTRY",
                qty=Decimal("0.001"),
                price=Decimal("62000"),
                status="FILLED",
                filled_qty=Decimal("0.001"),
                avg_fill_price=Decimal("62000"),
                fees=Decimal("0.06"),
            )
        )
        s.add(
            Position(
                id=uuid.uuid4(),
                broker_account_id=account_id,
                symbol="BTCUSDT",
                qty=Decimal("0.001"),
                avg_entry=Decimal("62000"),
                mark_price=Decimal("62500"),
                unrealized_pnl=Decimal("0.5"),
            )
        )
        s.add_all(
            [
                EquitySnapshot(
                    id=uuid.uuid4(), broker_account_id=account_id,
                    equity=Decimal("10000"), free_balance=Decimal("9000"),
                    taken_at=now - timedelta(hours=1), is_session_baseline=True,
                ),
                EquitySnapshot(
                    id=uuid.uuid4(), broker_account_id=account_id,
                    equity=Decimal("10050"), free_balance=Decimal("9000"),
                    taken_at=now, is_session_baseline=False,
                ),
            ]
        )
        await s.commit()

    orders = (await client.get("/api/orders")).json()
    assert len(orders) == 1
    assert orders[0]["symbol"] == "BTCUSDT"
    assert orders[0]["qty"] == "0.001000000000000000"

    positions = (await client.get("/api/positions")).json()
    assert len(positions) == 1
    assert positions[0]["unrealized_pnl"] == "0.500000000000000000"

    curve = (await client.get("/api/equity-curve")).json()
    assert len(curve) == 2
    # Oldest first — the order a chart plots.
    assert curve[0]["is_session_baseline"] is True
    assert curve[1]["equity"] == "10050.000000000000000000"
