"""Per-user notification settings API."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.api.conftest import register

pytestmark = pytest.mark.integration


async def test_default_chat_id_is_null(client: AsyncClient) -> None:
    await register(client)
    body = (await client.get("/api/notifications")).json()
    assert body["telegram_chat_id"] is None


async def test_set_and_read_back(client: AsyncClient) -> None:
    await register(client)
    updated = await client.put("/api/notifications", json={"telegram_chat_id": "123456"})
    assert updated.status_code == 200
    assert updated.json()["telegram_chat_id"] == "123456"
    # Persisted.
    assert (await client.get("/api/notifications")).json()["telegram_chat_id"] == "123456"


async def test_negative_chat_id_allowed(client: AsyncClient) -> None:
    """Group/channel chat ids are negative (e.g. -1001234567890)."""
    await register(client)
    r = await client.put("/api/notifications", json={"telegram_chat_id": "-1001234567890"})
    assert r.status_code == 200
    assert r.json()["telegram_chat_id"] == "-1001234567890"


async def test_empty_string_clears(client: AsyncClient) -> None:
    await register(client)
    await client.put("/api/notifications", json={"telegram_chat_id": "123"})
    cleared = await client.put("/api/notifications", json={"telegram_chat_id": ""})
    assert cleared.json()["telegram_chat_id"] is None


async def test_non_numeric_rejected(client: AsyncClient) -> None:
    await register(client)
    r = await client.put("/api/notifications", json={"telegram_chat_id": "not-a-number"})
    assert r.status_code == 422


async def test_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/notifications")).status_code == 401
