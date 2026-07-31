"""Kill switch and unlock.

Two paths are covered: the default (no broker wired) where the endpoint's job is
to lock the account durably, and the injected-broker path where it drives the
full, already-tested `fire_kill_switch` sweep. The fake broker keeps the test off
any network (constraint from §13).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient

from signalguard.api.routes_killswitch import provide_kill_switch_broker
from signalguard.execution.base import BrokerPosition
from tests.api.conftest import register
from tests.fakes.fake_broker import FakeBroker

pytestmark = pytest.mark.integration

CREATE = {"label": "acct", "api_key": "k", "api_secret": "s"}


async def _make_account(client: AsyncClient) -> str:
    await register(client)
    return str((await client.post("/api/broker-accounts", json=CREATE)).json()["id"])


async def test_kill_locks_the_account_without_a_broker(client: AsyncClient) -> None:
    account_id = await _make_account(client)
    response = await client.post(f"/api/broker-accounts/{account_id}/kill")
    assert response.status_code == 200
    body = response.json()
    assert body["trading_state"] == "LOCKED"
    assert body["locked_at"] is not None
    # No adapter available, so no immediate sweep was performed.
    assert body["swept"] is False

    # The lock is visible on the account resource too.
    listed = (await client.get("/api/broker-accounts")).json()
    assert listed[0]["trading_state"] == "LOCKED"


async def test_kill_with_broker_sweeps_positions(
    app: Any, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Keep the two-sweep settle time from slowing the test.
    monkeypatch.setattr("signalguard.execution.killswitch.SETTLE_SECONDS", 0.0)

    account_id = await _make_account(client)

    fake = FakeBroker(
        positions={
            "BTCUSDT": BrokerPosition(
                symbol="BTCUSDT",
                qty=Decimal("0.01"),
                avg_entry=Decimal("62000"),
                mark_price=Decimal("62000"),
            )
        }
    )
    app.dependency_overrides[provide_kill_switch_broker] = lambda: fake
    try:
        response = await client.post(f"/api/broker-accounts/{account_id}/kill")
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert body["trading_state"] == "LOCKED"
    assert body["swept"] is True
    assert body["positions_closed"] == 1
    assert fake.close_all_calls >= 1


async def test_kill_is_idempotent(client: AsyncClient) -> None:
    account_id = await _make_account(client)
    first = await client.post(f"/api/broker-accounts/{account_id}/kill")
    second = await client.post(f"/api/broker-accounts/{account_id}/kill")
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["trading_state"] == "LOCKED"


async def test_unlock_clears_the_lock(client: AsyncClient) -> None:
    account_id = await _make_account(client)
    await client.post(f"/api/broker-accounts/{account_id}/kill")
    unlocked = await client.post(f"/api/broker-accounts/{account_id}/unlock")
    assert unlocked.status_code == 200
    body = unlocked.json()
    assert body["trading_state"] == "ACTIVE"
    assert body["locked_at"] is None


async def test_kill_on_another_users_account_is_404(
    client: AsyncClient, make_client: object
) -> None:
    account_id = await _make_account(client)
    async with make_client() as other:  # type: ignore[operator]
        await register(other)
        assert (
            await other.post(f"/api/broker-accounts/{account_id}/kill")
        ).status_code == 404


async def test_kill_on_unknown_account_is_404(client: AsyncClient) -> None:
    await register(client)
    assert (
        await client.post(f"/api/broker-accounts/{uuid.uuid4()}/kill")
    ).status_code == 404


async def test_kill_requires_auth(client: AsyncClient) -> None:
    assert (
        await client.post(f"/api/broker-accounts/{uuid.uuid4()}/kill")
    ).status_code == 401
