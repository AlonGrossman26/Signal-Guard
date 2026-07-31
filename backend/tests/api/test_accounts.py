"""Broker-account and webhook-endpoint CRUD, and their credential handling."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.api.conftest import register

pytestmark = pytest.mark.integration

CREATE = {
    "label": "binance-testnet-1",
    "api_key": "AK-secret-key",
    "api_secret": "AS-secret-secret",
}


async def test_create_returns_account_without_credentials(client: AsyncClient) -> None:
    await register(client)
    response = await client.post("/api/broker-accounts", json=CREATE)
    assert response.status_code == 201
    body = response.json()
    assert body["label"] == "binance-testnet-1"
    assert body["is_testnet"] is True
    assert body["trading_state"] == "ACTIVE"
    # The credentials must never appear in a response.
    assert "api_key" not in response.text
    assert "api_secret" not in response.text
    assert "AS-secret-secret" not in response.text


async def test_duplicate_label_rejected(client: AsyncClient) -> None:
    await register(client)
    assert (await client.post("/api/broker-accounts", json=CREATE)).status_code == 201
    again = await client.post("/api/broker-accounts", json=CREATE)
    assert again.status_code == 409


async def test_list_only_returns_own_accounts(
    client: AsyncClient, make_client: object
) -> None:
    await register(client)
    await client.post("/api/broker-accounts", json=CREATE)

    async with make_client() as other:  # type: ignore[operator]
        await register(other)
        assert (await other.get("/api/broker-accounts")).json() == []

    mine = (await client.get("/api/broker-accounts")).json()
    assert len(mine) == 1


async def test_delete_removes_the_account(client: AsyncClient) -> None:
    await register(client)
    account_id = (await client.post("/api/broker-accounts", json=CREATE)).json()["id"]
    assert (
        await client.delete(f"/api/broker-accounts/{account_id}")
    ).status_code == 204
    assert (await client.get("/api/broker-accounts")).json() == []


async def test_cannot_delete_a_locked_account(client: AsyncClient) -> None:
    await register(client)
    account_id = (await client.post("/api/broker-accounts", json=CREATE)).json()["id"]
    # Lock it with the kill switch (no broker wired → durable lock only).
    killed = await client.post(f"/api/broker-accounts/{account_id}/kill")
    assert killed.json()["trading_state"] == "LOCKED"
    # Now deletion must be refused.
    assert (
        await client.delete(f"/api/broker-accounts/{account_id}")
    ).status_code == 409


async def test_another_users_account_is_404_not_403(
    client: AsyncClient, make_client: object
) -> None:
    await register(client)
    account_id = (await client.post("/api/broker-accounts", json=CREATE)).json()["id"]

    async with make_client() as other:  # type: ignore[operator]
        await register(other)
        # Someone else's ID must look identical to a non-existent one.
        assert (
            await other.delete(f"/api/broker-accounts/{account_id}")
        ).status_code == 404


async def test_webhook_endpoint_reveals_secrets_exactly_once(
    client: AsyncClient,
) -> None:
    await register(client)
    created = await client.post("/api/webhook-endpoints")
    assert created.status_code == 201
    body = created.json()
    # The one and only time the caller sees these.
    assert body["endpoint_token"]
    assert body["hmac_secret"]
    assert body["body_secret"]

    # The list view must never carry the token or secrets.
    listed = (await client.get("/api/webhook-endpoints")).json()
    assert len(listed) == 1
    assert "endpoint_token" not in listed[0]
    assert "hmac_secret" not in listed[0]
    assert body["endpoint_token"] not in (await client.get("/api/webhook-endpoints")).text


async def test_accounts_require_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/broker-accounts")).status_code == 401
    assert (await client.post("/api/webhook-endpoints")).status_code == 401
