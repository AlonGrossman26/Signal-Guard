"""FastAPI application factory and startup wiring."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession

from signalguard import __version__
from signalguard.api.health import router as health_router
from signalguard.api.routes_accounts import router as accounts_router
from signalguard.api.routes_auth import router as auth_router
from signalguard.api.routes_feed import router as feed_router
from signalguard.api.routes_killswitch import router as killswitch_router
from signalguard.api.routes_notifications import router as notifications_router
from signalguard.api.routes_profile import router as profile_router
from signalguard.api.routes_ws import router as ws_router
from signalguard.config import Settings, get_settings
from signalguard.db.models import BrokerAccount
from signalguard.db.session import dispose_engine, get_session, init_engine
from signalguard.execution.base import BrokerAdapter
from signalguard.execution.reconciler import reconciliation_loop
from signalguard.ingress.routes import router as webhook_router
from signalguard.logging import configure_logging, register_secret_value
from signalguard.notify import Notifier
from signalguard.redis_client import close_redis, get_redis, init_redis
from signalguard.wiring import build_broker_for, notifier_for_account

logger = logging.getLogger(__name__)

# How long shutdown waits for the reconciler to finish its current cycle. Long
# enough for an in-flight broker call to return, short enough that a container
# stop does not hang.
_RECONCILER_SHUTDOWN_SEC = 20.0


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

    stop_event = asyncio.Event()
    reconciler: asyncio.Task[None] | None = None
    if settings.reconciler_enabled:
        reconciler = asyncio.create_task(
            _run_reconciler(settings, stop_event), name="reconciler"
        )
        logger.info(
            "Reconciliation loop started",
            extra={"interval_sec": settings.reconciler_interval_sec},
        )
    else:
        # Only ever off deliberately. Say so loudly: without the loop, order
        # repair, position sync, equity snapshots, closed trades and the
        # continuous LOCKED enforcement all stop happening.
        logger.warning(
            "Reconciliation loop is DISABLED — broker state will not be repaired"
        )

    try:
        yield
    finally:
        stop_event.set()
        if reconciler is not None:
            try:
                await asyncio.wait_for(reconciler, timeout=_RECONCILER_SHUTDOWN_SEC)
            except (TimeoutError, asyncio.CancelledError):
                reconciler.cancel()
                logger.warning("Reconciliation loop did not stop cleanly; cancelled")
        await dispose_engine()
        await close_redis()
        logger.info("SignalGuard stopped")


async def _run_reconciler(settings: Settings, stop_event: asyncio.Event) -> None:
    """Drive the reconciliation loop for the life of the process (CLAUDE.md §10).

    Built here, in the composition root, because the loop takes its session and
    broker factories by injection — that is what lets it be driven by a fake
    broker in tests without ever reaching a network.
    """

    async def broker_factory(account: BrokerAccount) -> BrokerAdapter:
        return await build_broker_for(account, settings.credentials_master_key)

    async def notifier_factory(
        session: AsyncSession, account: BrokerAccount
    ) -> Notifier | None:
        return await notifier_for_account(session, account, settings)

    await reconciliation_loop(
        get_session,
        broker_factory,
        settings.reconciler_interval_sec,
        stop_event=stop_event,
        redis=get_redis(),
        notifier_factory=notifier_factory,
    )


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

    # The dashboard calls the API cross-origin in dev (Next.js on :3000) with a
    # session cookie, so credentialed CORS is required. allow_credentials with a
    # concrete origin list — never "*", which browsers reject for credentialed
    # requests and which would be unsafe for a money-moving app anyway.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(webhook_router)
    # Dashboard REST API (CLAUDE.md §11). All under /api, session-authenticated.
    app.include_router(auth_router)
    app.include_router(profile_router)
    app.include_router(accounts_router)
    app.include_router(feed_router)
    app.include_router(killswitch_router)
    app.include_router(notifications_router)
    app.include_router(ws_router)
    return app


app = create_app()
