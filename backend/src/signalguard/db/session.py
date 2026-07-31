"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create the process-wide engine. Called once, from the app lifespan."""
    global _engine, _sessionmaker
    _engine = create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,  # a connection killed by a restart is replaced, not raised
        pool_size=10,
        max_overflow=5,
    )
    _sessionmaker = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine not initialised — call init_engine() first.")
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that rolls back on error.

    Rollback-on-exception is not merely tidy: a half-written decision row would
    be an audit record of something that never happened.
    """
    if _sessionmaker is None:
        raise RuntimeError("Database engine not initialised — call init_engine() first.")
    async with _sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
