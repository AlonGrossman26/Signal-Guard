"""Health endpoints.

Two endpoints, because they answer two different questions:

* **/health/live** — "is this process running?" Used by the container
  healthcheck and any future orchestrator. It must NOT check dependencies: if
  Postgres blips, restarting the API does not help, and a liveness probe that
  fails on a dependency outage turns a brief database hiccup into a restart
  loop.
* **/health** — "can this service actually do its job?" Checks Postgres and
  Redis and returns 503 if either is unreachable. This is the one the phase gate
  cares about, and it reports honestly rather than returning 200 whenever the
  web framework happens to be up.

Reporting a degraded dependency as healthy would be the monitoring equivalent of
"assume and proceed".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from signalguard import __version__
from signalguard.db.session import get_engine
from signalguard.redis_client import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

# A dependency that has not answered in this long is treated as down. Generous
# enough not to trip on a slow local Docker start, short enough that the health
# endpoint itself never hangs.
_CHECK_TIMEOUT_SEC = 3.0


async def _check_postgres() -> dict[str, Any]:
    try:
        engine = get_engine()
        async with asyncio.timeout(_CHECK_TIMEOUT_SEC):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        return {"status": "up"}
    except TimeoutError:
        return {"status": "down", "error": f"timeout after {_CHECK_TIMEOUT_SEC}s"}
    except Exception as exc:  # noqa: BLE001 - any failure means "down"
        # Only the exception type, never the message: a SQLAlchemy connection
        # error stringifies the DSN, password included.
        return {"status": "down", "error": type(exc).__name__}


async def _check_redis() -> dict[str, Any]:
    try:
        client = get_redis()
        async with asyncio.timeout(_CHECK_TIMEOUT_SEC):
            await client.ping()
        return {"status": "up"}
    except TimeoutError:
        return {"status": "down", "error": f"timeout after {_CHECK_TIMEOUT_SEC}s"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "down", "error": type(exc).__name__}


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    """Process liveness only. Never touches a dependency."""
    return {"status": "alive", "version": __version__}


@router.get("/health")
async def health(response: Response) -> dict[str, Any]:
    """Full readiness check: green only when every dependency answers."""
    postgres, redis_state = await asyncio.gather(_check_postgres(), _check_redis())

    checks = {"postgres": postgres, "redis": redis_state}
    all_up = all(check["status"] == "up" for check in checks.values())
    overall: Literal["green", "degraded"] = "green" if all_up else "degraded"

    if not all_up:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        down = [name for name, c in checks.items() if c["status"] != "up"]
        logger.warning("Health check degraded", extra={"dependencies_down": down})

    return {"status": overall, "version": __version__, "checks": checks}
