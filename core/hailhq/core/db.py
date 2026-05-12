"""Async SQLAlchemy engine + session helpers.

Lives in ``core`` (not ``api``) so the voicebot worker shares the same
engine + session factory rather than duplicating the wiring. The
sessionmaker is built lazily on first use so imports stay cheap for
tests that override ``get_session`` and never touch the real engine.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.core.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


_SCHEME_ALIASES = (
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql://",
    "postgres://",  # Heroku/Neon/Supabase emit this; SQLAlchemy doesn't accept it as-is
)


def _rewrite_scheme(url: str, target_prefix: str) -> str:
    """Strip whichever Postgres scheme alias ``url`` starts with and prepend ``target_prefix``."""
    for alias in _SCHEME_ALIASES:
        if url.startswith(alias):
            return target_prefix + url[len(alias) :]
    return url


def to_async_url(url: str) -> str:
    """Coerce a ``DATABASE_URL`` onto the ``asyncpg`` driver used at runtime."""
    return _rewrite_scheme(url, "postgresql+asyncpg://")


def to_sync_url(url: str) -> str:
    """Coerce a ``DATABASE_URL`` onto the ``psycopg`` (sync) driver used by alembic."""
    return _rewrite_scheme(url, "postgresql+psycopg://")


def _ensure_initialized() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(to_async_url(settings.database_url))
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
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
