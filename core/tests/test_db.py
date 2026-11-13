"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from urllib.parse import parse_qs, urlsplit

import pytest
from hailhq.core.db import to_async_url, to_sync_url
from pydantic import ValidationError
from sqlalchemy import text


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "capacity"),
    [
        ({}, 2),
        ({"database_pool_size": 1}, 1),
        ({"database_pool_size": 1, "database_max_overflow": 1}, 2),
    ],
)
async def test_runtime_pool_waits_instead_of_exceeding_its_connection_limit(
    database_url, monkeypatch, options, capacity
):
    from hailhq.core import db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db.settings, "database_url", database_url)
    for name, value in options.items():
        monkeypatch.setattr(db.settings, name, value)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: False)
    factory = db._ensure_initialized()

    async def read_one():
        async with factory() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    try:
        async with AsyncExitStack() as holders:
            for _ in range(capacity):
                session = await holders.enter_async_context(factory())
                await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=2)
            # All runtime slots are in use. Extra work must wait rather
            # than exceed this process's share of the session pooler.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_one(), timeout=0.5)
        # A canceled waiter must not poison the pool or retain a slot.
        assert await asyncio.wait_for(read_one(), timeout=2) == 1
    finally:
        await db.dispose_engine()


@pytest.mark.parametrize(
    "options", [{"database_pool_size": 0}, {"database_max_overflow": -1}]
)
def test_runtime_pool_rejects_unlimited_connection_settings(options):
    from hailhq.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


@pytest.mark.asyncio
async def test_independent_commits_share_only_one_additional_connection(
    database_url, monkeypatch
):
    from hailhq.core import db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db, "_independent_engine", None)
    monkeypatch.setattr(db, "_independent_sessionmaker", None)
    monkeypatch.setattr(db.settings, "database_url", database_url)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: False)

    async def read_one():
        async with db.independent_session_scope() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    try:
        async with db.independent_session_scope() as first:
            await first.execute(text("SELECT 1"))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_one(), timeout=0.5)
        assert await asyncio.wait_for(read_one(), timeout=2) == 1
    finally:
        await db.dispose_engine()


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


@pytest.mark.asyncio
async def test_retry_on_pool_timeout_waits_out_a_busy_pool(database_url, monkeypatch):
    """A write that times out waiting for a connection runs again and
    succeeds once the holder lets go."""
    from hailhq.core import db
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        to_async_url(database_url), pool_size=1, max_overflow=0, pool_timeout=0.2
    )
    monkeypatch.setattr(
        db, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
    )

    async def read_one():
        async with db.session_scope() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    async def hold(seconds):
        async with db.session_scope() as session:
            await session.execute(text("SELECT 1"))
            await asyncio.sleep(seconds)

    try:
        holder = asyncio.create_task(hold(0.3))
        await asyncio.sleep(0.05)
        with pytest.raises(PoolTimeout):
            await read_one()
        assert await db.retry_on_pool_timeout(read_one) == 1
        await holder

        holder = asyncio.create_task(hold(1))
        await asyncio.sleep(0.05)
        with pytest.raises(PoolTimeout):
            await db.retry_on_pool_timeout(read_one, tries=2)
        await holder
    finally:
        await engine.dispose()
