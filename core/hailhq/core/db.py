"""Async SQLAlchemy engine + session helpers.

Lives in ``core`` (not ``api``) so the voicebot worker shares the same
engine + session factory rather than duplicating the wiring. The
sessionmaker is built lazily on first use so imports stay cheap for
tests that override ``get_session`` and never touch the real engine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import logfire
from hailhq.core.config import SELF_HOSTED_ORG_ID, settings
from hailhq.core.telemetry import instrument, telemetry_enabled
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_guard_engine: AsyncEngine | None = None
_guard_sessionmaker: async_sessionmaker[AsyncSession] | None = None


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


def _ensure_initialized() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(to_async_url(settings.database_url))
        if telemetry_enabled():
            # Tracing must never take the database down: the instrumentation
            # refused SQLAlchemy 2.1 and failed every request.
            instrument("SQLAlchemy", logfire.instrument_sqlalchemy, engine=_engine)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _guard_engine, _guard_sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    if _guard_engine is not None:
        await _guard_engine.dispose()
    _guard_engine = None
    _guard_sessionmaker = None


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
async def organization_guard_scope() -> AsyncIterator[AsyncSession]:
    """A separate pool prevents admission guards from starving route queries."""
    global _guard_engine, _guard_sessionmaker
    if _guard_sessionmaker is None:
        _guard_engine = create_async_engine(to_async_url(settings.database_url))
        _guard_sessionmaker = async_sessionmaker(_guard_engine, expire_on_commit=False)
    async with _guard_sessionmaker() as session:
        yield session


async def org_lock(db: AsyncSession, org: UUID) -> None:
    """Hold this organization's lock until the transaction ends. Billing
    and number writes of one organization take it, so they run in order."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(org)},
    )


async def lock_open_organization(db: AsyncSession, org: UUID) -> bool:
    """Drain mutations before deletion, including across external I/O.

    The website holds FOR UPDATE on the org for its entire delete flow.
    A caller holds this KEY SHARE lock until its transaction ends. HTTP
    callers use a separate session so route commits do not release it early.
    Inbound writers use their ingest transaction. A tombstone rejects callers
    that waited for deletion and callers retrying while cleanup is paused.
    """
    from hailhq.core.models import Organization, OrgClosure
    from sqlalchemy import select

    if org == SELF_HOSTED_ORG_ID:
        return True

    await db.execute(
        select(Organization.id)
        .where(Organization.id == org)
        .with_for_update(read=True, key_share=True)
    )
    closure = await db.get(OrgClosure, org)
    return closure is None or closure.source != "organization_delete"
