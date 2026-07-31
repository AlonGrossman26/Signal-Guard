"""Risk-profile read and update."""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.api.conftest import register

pytestmark = pytest.mark.integration


async def test_get_returns_defaults(client: AsyncClient) -> None:
    await register(client)
    profile = (await client.get("/api/risk-profile")).json()
    assert profile["version"] == 1
    # Money and percentages are strings, never floats (constraint #3).
    assert profile["risk_per_trade_pct"] == "0.010000"
    assert isinstance(profile["max_notional_per_trade"], str)


async def test_partial_update_bumps_version_and_changes_only_named_fields(
    client: AsyncClient,
) -> None:
    await register(client)
    before = (await client.get("/api/risk-profile")).json()

    updated = await client.put(
        "/api/risk-profile",
        json={"allowed_symbols": ["btcusdt", "ETHUSDT"], "max_open_positions": 5},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["version"] == before["version"] + 1
    # Symbols normalised to upper case.
    assert body["allowed_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert body["max_open_positions"] == 5
    # An untouched field is unchanged.
    assert body["risk_per_trade_pct"] == before["risk_per_trade_pct"]


async def test_money_field_rejects_a_json_float(client: AsyncClient) -> None:
    await register(client)
    # 0.02 as a bare JSON number is a float — forbidden for a risk parameter.
    response = await client.put("/api/risk-profile", json={"risk_per_trade_pct": 0.02})
    assert response.status_code == 422
    # The same value as a string is accepted.
    ok = await client.put("/api/risk-profile", json={"risk_per_trade_pct": "0.02"})
    assert ok.status_code == 200
    assert ok.json()["risk_per_trade_pct"] == "0.020000"


async def test_percentage_out_of_range_rejected(client: AsyncClient) -> None:
    await register(client)
    # 50 (meaning 50%, but stored as a fraction it is nonsense) must be refused.
    response = await client.put("/api/risk-profile", json={"risk_per_trade_pct": "50"})
    assert response.status_code == 422


async def test_unknown_timezone_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await client.put(
        "/api/risk-profile", json={"timezone": "Mars/Olympus_Mons"}
    )
    assert response.status_code == 422


async def test_valid_timezone_and_reset_time_accepted(client: AsyncClient) -> None:
    await register(client)
    response = await client.put(
        "/api/risk-profile",
        json={"timezone": "America/New_York", "daily_reset_time": "09:30:00"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "America/New_York"
    assert body["daily_reset_time"] == "09:30"


async def test_empty_update_is_rejected(client: AsyncClient) -> None:
    await register(client)
    assert (await client.put("/api/risk-profile", json={})).status_code == 400


async def test_unknown_field_is_rejected(client: AsyncClient) -> None:
    await register(client)
    response = await client.put("/api/risk-profile", json={"leverage": 10})
    assert response.status_code == 422


async def test_profile_requires_auth(client: AsyncClient) -> None:
    assert (await client.get("/api/risk-profile")).status_code == 401
