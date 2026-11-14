"""Async SQLAlchemy engine + session helpers.

Lives in ``core`` (not ``api``) so the voicebot worker shares the same
engine + session factory rather than duplicating the wiring. The
sessionmaker is built lazily on first use so imports stay cheap for
tests that override ``get_session`` and never touch the real engine.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import logfire
from hailhq.core.config import settings
from hailhq.core.telemetry import instrument, telemetry_enabled
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_independent_engine: AsyncEngine | None = None
_independent_sessionmaker: async_sessionmaker[AsyncSession] | None = None


_SCHEME_ALIASES = (
    "postgresql+asyncpg://",
    "postgresql+psycopg://",
    "postgresql+psycopg2://",
    "postgresql://",
    "postgres://",  # Heroku/Neon/Supabase emit this; SQLAlchemy doesn't accept it as-is
)

# libpq query-string params asyncpg's ``connect()`` rejects, mapped to their
# asyncpg equivalent (``None`` ⇒ drop). Managed Postgres URLs (Neon, Supabase,
# Heroku) commonly carry these. ``sslmode`` accepts the same string values
# ('disable', 'prefer', 'require', ...) under asyncpg's ``ssl`` kwarg.
_LIBPQ_TO_ASYNCPG: dict[str, str | None] = {
    "sslmode": "ssl",
    "channel_binding": None,
    "gssencmode": None,
    "sslcert": None,
    "sslkey": None,
    "sslrootcert": None,
    "sslcrl": None,
    "options": None,
    "application_name": None,
    "krbsrvname": None,
}


def _rewrite_scheme(url: str, target_prefix: str) -> str:
    """Strip whichever Postgres scheme alias ``url`` starts with and prepend ``target_prefix``."""
    for alias in _SCHEME_ALIASES:
        if url.startswith(alias):
            return target_prefix + url[len(alias) :]
    return url


def _sanitize_asyncpg_query(query: str) -> str:
    """Translate ``sslmode`` to ``ssl`` and drop other libpq-only params asyncpg rejects."""
    out: list[tuple[str, str]] = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key in _LIBPQ_TO_ASYNCPG:
            mapped = _LIBPQ_TO_ASYNCPG[key]
            if mapped is not None:
                out.append((mapped, value))
        else:
            out.append((key, value))
    return urlencode(out)


def to_async_url(url: str) -> str:
    """Coerce a ``DATABASE_URL`` onto the ``asyncpg`` driver used at runtime.

    Also strips libpq-only query params that asyncpg's ``connect()`` rejects —
    notably ``sslmode`` (translated to ``ssl``) and ``channel_binding`` — so
    managed-Postgres URLs from Neon, Supabase, Heroku, etc. work unchanged.
    """
    rewritten = _rewrite_scheme(url, "postgresql+asyncpg://")
    parts = urlsplit(rewritten)
    if not parts.query:
        return rewritten
    return urlunsplit(parts._replace(query=_sanitize_asyncpg_query(parts.query)))


def to_sync_url(url: str) -> str:
    """Coerce a ``DATABASE_URL`` onto the ``psycopg`` (sync) driver used by alembic."""
    return _rewrite_scheme(url, "postgresql+psycopg://")


def _create_session_pool(
    size: int, overflow: int
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        to_async_url(settings.database_url), pool_size=size, max_overflow=overflow
    )
    if telemetry_enabled():
        # Unsupported tracing must never take the database down.
        instrument("SQLAlchemy", logfire.instrument_sqlalchemy, engine=engine)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _ensure_initialized() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine, _sessionmaker = _create_session_pool(
            settings.database_pool_size, settings.database_max_overflow
        )
    return _sessionmaker


def _ensure_independent_initialized() -> async_sessionmaker[AsyncSession]:
    global _independent_engine, _independent_sessionmaker
    if _independent_sessionmaker is None:
        # Audit, usage, and idempotency commits can run while a request holds
        # a transaction. Separate slots prevent nested-checkout starvation.
        _independent_engine, _independent_sessionmaker = _create_session_pool(2, 0)
    return _independent_sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _independent_engine, _independent_sessionmaker
    engines = (_engine, _independent_engine)
    # Reset first: a failed dispose must not keep later pools or stale globals.
    _engine = None
    _sessionmaker = None
    _independent_engine = None
    _independent_sessionmaker = None
    for engine in engines:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                logger.warning("database pool dispose failed", exc_info=True)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a per-request ``AsyncSession``."""
    async with _ensure_initialized()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Open a fresh ``AsyncSession`` outside any FastAPI request scope."""
    async with _ensure_initialized()() as session:
        yield session


@asynccontextmanager
async def independent_session_scope() -> AsyncIterator[AsyncSession]:
    """Short independent commits that must not wait for a request's slot.

    Never nest this scope inside another independent scope. Its two slots
    are reserved for audit, usage, and idempotency writes, without network
    work.
    """
    async with _ensure_independent_initialized()() as session:
        yield session


async def key_lock(db: AsyncSession, key: str) -> None:
    """Hold an advisory lock on ``key`` until the transaction ends. For
    short work that must run in order but must not wait on ``org_lock``
    (held through carrier calls), prefix the key so it never matches a
    bare organization id."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": key},
    )


async def org_lock(db: AsyncSession, org: UUID) -> None:
    """Hold this organization's lock until the transaction ends. Billing
    and number writes of one organization take it, so they run in order.
    The key is the bare organization id, as ``try_org_lock`` uses."""
    await key_lock(db, str(org))


async def try_org_lock(db: AsyncSession, org: UUID) -> bool:
    """``org_lock`` without waiting: False when another transaction holds
    it. For work that can skip a turn and try again later."""
    return bool(
        (
            await db.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": str(org)},
            )
        ).scalar_one()
    )
