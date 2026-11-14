"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from hailhq.core.db import to_async_url, to_sync_url


def test_to_async_url_rewrites_scheme_aliases():
    assert to_async_url("postgres://u:p@h/db").startswith("postgresql+asyncpg://")
    assert to_async_url("postgresql://u:p@h/db").startswith("postgresql+asyncpg://")
    assert to_async_url("postgresql+psycopg://u:p@h/db").startswith(
        "postgresql+asyncpg://"
    )
    # Already asyncpg — left alone.
    assert (
        to_async_url("postgresql+asyncpg://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    )


def test_to_async_url_translates_sslmode_to_ssl():
    rewritten = to_async_url("postgres://u:p@h/db?sslmode=require")
    parts = urlsplit(rewritten)
    qs = parse_qs(parts.query)
    assert qs == {"ssl": ["require"]}
    assert "sslmode" not in parts.query


def test_to_async_url_drops_channel_binding():
    rewritten = to_async_url(
        "postgres://u:p@h/db?sslmode=require&channel_binding=require"
    )
    qs = parse_qs(urlsplit(rewritten).query)
    assert qs == {"ssl": ["require"]}


def test_to_async_url_drops_other_libpq_only_params():
    rewritten = to_async_url(
        "postgres://u:p@h/db?sslmode=require&application_name=myapp&options=-c+geqo%3Doff"
    )
    qs = parse_qs(urlsplit(rewritten).query)
    assert qs == {"ssl": ["require"]}


def test_to_async_url_preserves_unknown_params():
    rewritten = to_async_url("postgres://u:p@h/db?prepared_statement_cache_size=0")
    qs = parse_qs(urlsplit(rewritten).query)
    assert qs == {"prepared_statement_cache_size": ["0"]}


def test_to_async_url_no_query_unchanged_apart_from_scheme():
    assert to_async_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


def test_to_sync_url_keeps_sslmode():
    # psycopg understands sslmode natively — don't touch it.
    rewritten = to_sync_url("postgres://u:p@h/db?sslmode=require")
    assert rewritten.startswith("postgresql+psycopg://")
    assert "sslmode=require" in rewritten


def test_a_tracing_failure_does_not_block_the_database(monkeypatch):
    from hailhq.core import db

    calls = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("could not instrument SQLAlchemy")

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(db.logfire, "instrument_sqlalchemy", refuse)
    try:
        assert db._ensure_initialized() is not None
        assert calls == [{"engine": db._engine}]
    finally:
        asyncio.run(db._engine.dispose())


async def test_org_lock_takes_the_same_key_as_before(session_factory) -> None:
    """org_lock goes through key_lock with the bare organization id, so the
    lock is the one every org_lock and try_org_lock caller already takes."""
    import uuid
    from unittest.mock import AsyncMock

    from hailhq.core import db as core_db

    org = uuid.uuid4()
    calls = []

    async def fake_key_lock(_session, key):
        calls.append(key)

    original = core_db.key_lock
    core_db.key_lock = fake_key_lock
    try:
        await core_db.org_lock(AsyncMock(), org)
    finally:
        core_db.key_lock = original
    assert calls == [str(org)]

    async with session_factory() as holder, session_factory() as other:
        await core_db.org_lock(holder, org)
        assert await core_db.try_org_lock(other, org) is False
        await holder.rollback()
        assert await core_db.try_org_lock(other, org) is True
        await other.rollback()


async def test_a_transaction_pooler_gets_no_named_statements(
    database_url, monkeypatch
) -> None:
    """A transaction-mode pooler keeps no prepared statements, so with the
    setting on the engine names none. Postgres lists named statements in
    ``pg_prepared_statements`` and never lists unnamed ones."""
    from hailhq.core import db
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def named_statements() -> list[str]:
        engine = create_async_engine(to_async_url(database_url), **db.engine_options())
        try:
            async with engine.connect() as conn:
                for n in range(3):
                    await conn.execute(text("SELECT CAST(:n AS integer)"), {"n": n})
                rows = await conn.execute(
                    text("SELECT name FROM pg_prepared_statements")
                )
                return [name for (name,) in rows]
        finally:
            await engine.dispose()

    monkeypatch.setattr(db.settings, "database_transaction_pooler", False)
    assert await named_statements()
    monkeypatch.setattr(db.settings, "database_transaction_pooler", True)
    assert await named_statements() == []
