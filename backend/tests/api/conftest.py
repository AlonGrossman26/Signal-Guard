"""Fixtures for the dashboard API tests.

These are integration tests: the API's guarantees — session revocation, the
per-user decision feed, the kill-switch lock — are produced by real SQL against
real tables, so faking the database would only test the fake. They boot the true
app against a live Postgres + Redis, exactly like the webhook e2e suite, and skip
when no database is configured.

Each test creates its own user through the real /register endpoint and uses a
fresh random email, so tests never collide and the append-only alert/decision
tables never need cleaning between them.
"""

from __future__ import annotations

import base64
import os
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

MASTER_KEY = base64.b64encode(secrets.token_bytes(32)).decode()
PEPPER = secrets.token_urlsafe(32)

PASSWORD = "a-perfectly-long-password"


@pytest.fixture()
async def app(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Boot the real FastAPI app against real dependencies."""
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")

    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", MASTER_KEY)
    monkeypatch.setenv("ENDPOINT_ID_PEPPER", PEPPER)
    monkeypatch.setenv("SESSION_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("APP_ENV", "test")

    from signalguard.config import get_settings
    from signalguard.db.session import dispose_engine, init_engine
    from signalguard.main import create_app
    from signalguard.redis_client import close_redis, init_redis

    get_settings.cache_clear()
    init_engine(DATABASE_URL)
    init_redis(REDIS_URL)

    application = create_app()

    # Insulate every API test from the network by default. The withdrawal check
    # and the kill-switch sweep both reach the exchange in production; tests that
    # care about those paths override these explicitly.
    from signalguard.api.routes_accounts import provide_key_checker
    from signalguard.api.routes_killswitch import provide_kill_switch_broker

    async def _unknown_permission(_key: str, _secret: str) -> None:
        return None

    application.dependency_overrides[provide_key_checker] = lambda: _unknown_permission
    application.dependency_overrides[provide_kill_switch_broker] = lambda: None

    yield application

    await dispose_engine()
    await close_redis()
    get_settings.cache_clear()


@pytest.fixture()
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    """An HTTP client with its own cookie jar (one logged-in user's browser)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture()
def make_client(app: Any) -> Callable[[], Any]:
    """Factory for extra independent clients — used to test cross-user isolation."""

    @asynccontextmanager
    async def _make() -> AsyncIterator[AsyncClient]:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    return _make


async def register(client: AsyncClient, email: str | None = None) -> str:
    """Register a fresh user and return their email. Leaves the client logged in."""
    email = email or f"{uuid.uuid4()}@example.test"
    response = await client.post(
        "/api/auth/register", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    return email


def db_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """A standalone session factory for seeding rows the API has no write path for.

    Orders, positions, decisions and equity snapshots are written by the ingress
    and execution layers, not by this API, so the read-model tests insert them
    directly.
    """
    engine = create_async_engine(DATABASE_URL)
    return async_sessionmaker(engine, expire_on_commit=False)
