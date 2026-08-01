"""FastAPI application factory and startup wiring."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from signalguard import __version__
from signalguard.api.health import router as health_router
from signalguard.api.routes_accounts import router as accounts_router
from signalguard.api.routes_auth import router as auth_router
from signalguard.api.routes_feed import router as feed_router
from signalguard.api.routes_killswitch import router as killswitch_router
from signalguard.api.routes_profile import router as profile_router
from signalguard.api.routes_ws import router as ws_router
from signalguard.config import Settings, get_settings
from signalguard.db.session import dispose_engine, init_engine
from signalguard.ingress.routes import router as webhook_router
from signalguard.logging import configure_logging, register_secret_value
from signalguard.redis_client import close_redis, init_redis

logger = logging.getLogger(__name__)


def _register_known_secrets(settings: Settings) -> None:
    """Teach the log redactor the literal secrets it should scrub.

    This is the backstop for secrets that reach a log through a path field-name
    matching cannot see — most often an exception message. A SQLAlchemy or Redis
    connection error stringifies the DSN, password included, and that traceback
    would otherwise land in the logs in full.
    """
    register_secret_value(settings.credentials_master_key)
    register_secret_value(settings.endpoint_id_pepper)
    register_secret_value(settings.session_secret)
    if settings.telegram_bot_token:
        register_secret_value(settings.telegram_bot_token)
    # The URLs themselves carry credentials in userinfo.
    register_secret_value(settings.database_url)
    register_secret_value(settings.redis_url)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop the connection pools.

    Note what this does NOT do: run migrations. Schema changes are an explicit,
    reviewed step (`alembic upgrade head`), not something that happens silently
    because a container restarted.
    """
    settings = get_settings()

    configure_logging(settings.log_level)
    _register_known_secrets(settings)

    init_engine(settings.database_url, echo=False)
    init_redis(settings.redis_url)

    logger.info(
        "SignalGuard starting",
        extra={
            "version": __version__,
            "app_env": settings.app_env,
            # Surfaced at every boot so "are we still on testnet?" is answerable
            # from the logs alone, without reading the config.
            "testnet_only": settings.is_testnet_only,
        },
    )

    try:
        yield
    finally:
        await dispose_engine()
        await close_redis()
        logger.info("SignalGuard stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="SignalGuard",
        version=__version__,
        description="Risk-management middleware between a signal source and a broker.",
        lifespan=lifespan,
        # No interactive docs outside development: the schema tells an attacker
        # exactly which endpoints exist and what they accept.
        docs_url="/docs" if settings.app_env == "local" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.app_env == "local" else None,
    )

    app.include_router(health_router)
    app.include_router(webhook_router)
    # Dashboard REST API (CLAUDE.md §11). All under /api, session-authenticated.
    app.include_router(auth_router)
    app.include_router(profile_router)
    app.include_router(accounts_router)
    app.include_router(feed_router)
    app.include_router(killswitch_router)
    app.include_router(ws_router)
    return app


app = create_app()
