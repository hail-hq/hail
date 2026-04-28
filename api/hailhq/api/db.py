"""Async SQLAlchemy engine + session helpers.

The sessionmaker is built lazily on first use so imports stay cheap for
tests that override ``get_session`` and never touch the real engine.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.core.config import settings

_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def to_async_url(url: str) -> str:
    """Coerce a ``DATABASE_URL`` to an asyncpg URL.

    Accepts the sync forms operators copy-paste from psql docs and
    rewrites them to use ``asyncpg``. ``+asyncpg`` URLs pass through.
    """
    if url.startswith("postgresql+asyncpg://"):
        return url
    for sync_prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(sync_prefix):
            return "postgresql+asyncpg://" + url[len(sync_prefix) :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _ensure_initialized() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        engine = create_async_engine(to_async_url(settings.database_url))
        _sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    global _sessionmaker
    if _sessionmaker is not None:
        await _sessionmaker.kw["bind"].dispose()
        _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a per-request ``AsyncSession``."""
    async with _ensure_initialized()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Open a fresh ``AsyncSession`` outside any FastAPI request scope."""
    async with _ensure_initialized()() as session:
        yield session
