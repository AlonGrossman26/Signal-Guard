"""Health endpoint behaviour.

The important assertion here is the unhappy one: when a dependency is down,
/health must say so with a 503 rather than returning a cheerful 200 because the
web framework itself is fine.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from signalguard.api import health as health_module
from signalguard.api.health import router


def _app() -> FastAPI:
    """A bare app with only the health router — no lifespan, no real connections."""
    app = FastAPI()
    app.include_router(router)
    return app


async def _get(path: str) -> tuple[int, dict[str, Any]]:
    transport = ASGITransport(app=_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path)
        return response.status_code, response.json()


async def test_liveness_needs_no_dependencies() -> None:
    """Liveness must not check Postgres or Redis.

    If it did, a database blip would fail the container healthcheck and trigger a
    restart loop — restarting the API does not fix a database.
    """
    status_code, body = await _get("/health/live")
    assert status_code == 200
    assert body["status"] == "alive"


async def test_health_green_when_all_dependencies_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def ok() -> dict[str, str]:
        return {"status": "up"}

    monkeypatch.setattr(health_module, "_check_postgres", ok)
    monkeypatch.setattr(health_module, "_check_redis", ok)

    status_code, body = await _get("/health")
    assert status_code == 200
    assert body["status"] == "green"
    assert body["checks"]["postgres"]["status"] == "up"
    assert body["checks"]["redis"]["status"] == "up"


@pytest.mark.parametrize("broken", ["_check_postgres", "_check_redis"])
async def test_health_degrades_with_503_when_a_dependency_is_down(
    monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    async def ok() -> dict[str, str]:
        return {"status": "up"}

    async def down() -> dict[str, str]:
        return {"status": "down", "error": "ConnectionError"}

    monkeypatch.setattr(health_module, "_check_postgres", ok)
    monkeypatch.setattr(health_module, "_check_redis", ok)
    monkeypatch.setattr(health_module, broken, down)

    status_code, body = await _get("/health")
    assert status_code == 503
    assert body["status"] == "degraded"


async def test_health_reports_error_type_not_message() -> None:
    """A connection error stringifies the DSN, password included. Type only."""
    # get_engine() raises RuntimeError here because no engine is initialised —
    # exactly the path a real failure takes.
    result = await health_module._check_postgres()
    assert result["status"] == "down"
    assert result["error"] == "RuntimeError"
    assert "postgresql" not in str(result).lower()
