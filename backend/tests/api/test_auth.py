"""Auth endpoints: registration, login, session lifetime, revocation."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.api.conftest import PASSWORD, register

pytestmark = pytest.mark.integration


async def test_register_creates_user_and_logs_in(client: AsyncClient) -> None:
    email = await register(client)
    # The cookie the register response set authenticates the very next call.
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email


async def test_register_also_creates_a_default_risk_profile(client: AsyncClient) -> None:
    await register(client)
    profile = await client.get("/api/risk-profile")
    assert profile.status_code == 200
    # Fail-closed default: a brand-new account may trade nothing.
    assert profile.json()["allowed_symbols"] == []


async def test_duplicate_email_is_rejected_without_confirming_it(
    client: AsyncClient,
) -> None:
    email = f"{uuid.uuid4()}@example.test"
    await register(client, email)
    again = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert again.status_code == 409
    # The message must not say "email already registered".
    assert "already" not in again.text.lower() or "account" in again.text.lower()


async def test_short_password_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/api/auth/register",
        json={"email": f"{uuid.uuid4()}@example.test", "password": "short"},
    )
    assert response.status_code == 422


async def test_me_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_login_wrong_password_is_401(client: AsyncClient) -> None:
    email = await register(client)
    # Drop the session so we are testing the password, not the cookie.
    await client.post("/api/auth/logout")
    bad = await client.post(
        "/api/auth/login", json={"email": email, "password": "wrong-password-entirely"}
    )
    assert bad.status_code == 401


async def test_login_unknown_email_is_401(client: AsyncClient) -> None:
    bad = await client.post(
        "/api/auth/login",
        json={"email": f"{uuid.uuid4()}@example.test", "password": PASSWORD},
    )
    assert bad.status_code == 401


async def test_login_success_restores_access(client: AsyncClient) -> None:
    email = await register(client)
    await client.post("/api/auth/logout")
    assert (await client.get("/api/auth/me")).status_code == 401

    good = await client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert good.status_code == 200
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    await register(client)
    assert (await client.get("/api/auth/me")).status_code == 200
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_email_is_normalised_case_insensitively(client: AsyncClient) -> None:
    local = uuid.uuid4().hex
    await register(client, f"{local}@Example.Test")
    await client.post("/api/auth/logout")
    # Logging in with different casing must reach the same account.
    good = await client.post(
        "/api/auth/login", json={"email": f"{local}@EXAMPLE.TEST", "password": PASSWORD}
    )
    assert good.status_code == 200
