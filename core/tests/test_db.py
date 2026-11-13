"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

import pytest
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


async def test_limit_pool_makes_sessions_wait_instead_of_opening_more(
    database_url, monkeypatch
):
    """Eight short sessions at once share two connections, so a managed pooler
    with a small client limit never sees more than two from this process."""
    from hailhq.core import db as core_db
    from hailhq.core.config import settings
    from sqlalchemy import text

    monkeypatch.setattr(settings, "database_url", database_url)
    monkeypatch.setattr(core_db, "_engine", None)
    monkeypatch.setattr(core_db, "_sessionmaker", None)
    monkeypatch.setattr(core_db, "_pool_options", {})
    core_db.limit_pool(2)
    peak = 0

    async def one() -> None:
        nonlocal peak
        async with core_db.session_scope() as session:
            await session.execute(text("SELECT pg_sleep(0.05)"))
            peak = max(peak, core_db._engine.pool.checkedout())

    try:
        await asyncio.gather(*(one() for _ in range(8)))
        assert peak == 2
        assert core_db._engine.pool.size() == 2
        with pytest.raises(RuntimeError):
            core_db.limit_pool(3)
    finally:
        await core_db.dispose_engine()
