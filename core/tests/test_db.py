"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

import asyncio
import contextvars
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
    monkeypatch.setattr(db, "_sqlalchemy_tracing_tried", False)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: True)
    monkeypatch.setattr(db.logfire, "instrument_sqlalchemy", refuse)
    try:
        assert db._ensure_initialized() is not None
        assert len(calls) == 1
        # A second pool does not retry or log the warning again.
        asyncio.run(db._engine.dispose())
        monkeypatch.setattr(db, "_engine", None)
        monkeypatch.setattr(db, "_sessionmaker", None)
        assert db._ensure_initialized() is not None
        assert len(calls) == 1
    finally:
        asyncio.run(db._engine.dispose())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "capacity"),
    [
        ({}, 5),
        ({"database_pool_size": 3}, 3),
        ({"database_pool_size": 2, "database_max_overflow": 1}, 3),
    ],
)
async def test_runtime_pool_waits_instead_of_exceeding_its_connection_limit(
    fresh_db_pools, monkeypatch, options, capacity
):
    from hailhq.core import db

    for name, value in options.items():
        monkeypatch.setattr(db.settings, name, value)
    factory = db._ensure_initialized()

    async def read_one():
        async with factory() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

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


@pytest.mark.parametrize(
    "options",
    [
        {"database_pool_size": 0},
        # Inbound call setup holds two sessions at once.
        {"database_pool_size": 1},
        {"database_max_overflow": -1},
    ],
)
def test_runtime_pool_rejects_unlimited_connection_settings(options):
    from hailhq.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, **options)


@pytest.mark.asyncio
async def test_independent_commits_share_two_additional_connections(
    fresh_db_pools,
):
    from hailhq.core import db

    async def read_one():
        async with db.independent_session_scope() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    async with AsyncExitStack() as holders:
        for _ in range(2):
            session = await holders.enter_async_context(db.independent_session_scope())
            await session.execute(text("SELECT 1"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(read_one(), timeout=0.5)
    assert await asyncio.wait_for(read_one(), timeout=2) == 1


@pytest.mark.asyncio
async def test_background_jobs_cannot_take_the_request_connections(fresh_db_pools):
    """A job holding every background connection (a slow carrier call)
    leaves the request pool free, and the other way round."""
    from hailhq.core import db

    capacity = db.settings.database_pool_size + db.settings.database_max_overflow

    def pool_context(background: bool) -> contextvars.Context:
        return db.background_context() if background else contextvars.copy_context()

    async def read_one():
        async with db.session_scope() as session:
            return (await session.execute(text("SELECT 1"))).scalar_one()

    def read_in(background: bool) -> asyncio.Task:
        return asyncio.create_task(read_one(), context=pool_context(background))

    async def hold_all(release: asyncio.Event, held: asyncio.Event):
        async with AsyncExitStack() as holders:
            for _ in range(capacity):
                session = await holders.enter_async_context(db.session_scope())
                await session.execute(text("SELECT 1"))
            held.set()
            await release.wait()

    for job_holds in (True, False):
        release, held = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(
            hold_all(release, held), context=pool_context(job_holds)
        )
        try:
            await asyncio.wait_for(held.wait(), timeout=2)
            # The holder's own pool is full, so one more read there waits.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(read_in(job_holds), timeout=0.5)
            assert await asyncio.wait_for(read_in(not job_holds), timeout=2) == 1
        finally:
            release.set()
            await holder


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


@pytest.mark.parametrize("semconv", ["", "database", "database/dup"])
def test_queries_from_every_pool_export_private_spans(database_url, semconv):
    """A dependency mismatch or a later pool must not silently lose query spans."""
    import os
    import subprocess
    import sys

    script = """
import asyncio
import logfire
from hailhq.core import db, telemetry
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from opentelemetry.trace import StatusCode
memory = InMemorySpanExporter()
telemetry.OTLPSpanExporter = lambda **kwargs: memory
telemetry.OTLPMetricExporter = lambda **kwargs: None
telemetry.PeriodicExportingMetricReader = lambda *args, **kwargs: InMemoryMetricReader()
assert telemetry.configure_telemetry("hail-api")

async def run():
    try:
        for generation in range(2):
            for name, scope in (("request", db.session_scope), ("independent", db.independent_session_scope), ("background", db.session_scope)):
                async def query():
                    with telemetry.operation(f"test.{generation}.{name}"):
                        async with scope() as session:
                            result = await session.execute(text("SELECT 'PRIVATE-SQL-LITERAL', :value"), {"value": "PRIVATE-SQL-PARAMETER"})
                            assert result.one() == ("PRIVATE-SQL-LITERAL", "PRIVATE-SQL-PARAMETER")
                            try:
                                await session.execute(text("SELECT CAST(:value AS INTEGER)"), {"value": "PRIVATE-SQL-PARAMETER"})
                            except DBAPIError:
                                await session.rollback()
                            else:
                                raise AssertionError("invalid integer must fail")
                if name == "background":
                    await asyncio.create_task(query(), context=db.background_context())
                else:
                    await query()
            await db.dispose_engine()
    finally:
        await db.dispose_engine()

asyncio.run(run())
telemetry.flush_telemetry()
spans = memory.get_finished_spans()
queries = [s for s in spans if s.instrumentation_scope.name == "opentelemetry.instrumentation.sqlalchemy" and s.name.startswith("SELECT")]
assert len(queries) == 12, [(s.name, dict(s.attributes)) for s in spans]
parents = {s.context.span_id for s in spans if s.name.startswith("test.")}
assert {s.parent.span_id for s in queries} == parents
assert all(s.end_time > s.start_time for s in queries)
assert all(s.attributes.get("db.system", s.attributes.get("db.system.name")) == "postgresql" for s in queries)
assert sum(s.status.status_code == StatusCode.ERROR for s in queries) == 6
for span in spans:
    exported = str((span.name, span.attributes, span.events, span.status.description))
    assert "PRIVATE-SQL" not in exported, exported
    assert "db.statement" not in span.attributes
    assert "db.query.text" not in span.attributes
    assert not any("parameter" in key for key in span.attributes)
assert not any("SQLAlchemy tracing is off" in str(s.attributes.get("logfire.msg", "")) for s in spans)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DATABASE_URL": database_url,
            "OTEL_SEMCONV_STABILITY_OPT_IN": semconv,
            "HAIL_LOGFIRE_ENABLED": "true",
            "HAIL_LOGFIRE_SAMPLE_RATE": "1",
            "LOGFIRE_TOKEN": "test-only-placeholder",
            "LOGFIRE_BASE_URL": "https://logfire.example.test",
        },
        text=True,
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "changed_package",
    [
        "opentelemetry-instrumentation-sqlalchemy",
        "logfire",
        "logfire-sdk",
    ],
)
def test_a_tracing_package_bump_keeps_the_sqlalchemy_bridge(
    monkeypatch, changed_package
):
    from hailhq.core import db

    original_version = db.version
    monkeypatch.setattr(
        db,
        "version",
        lambda package: (
            "999" if package == changed_package else original_version(package)
        ),
    )
    calls = []
    monkeypatch.setattr(
        db.logfire, "instrument_sqlalchemy", lambda **kw: calls.append(kw)
    )
    db._instrument_sqlalchemy()
    assert calls == [{"skip_dep_check": True}]


@pytest.mark.filterwarnings("ignore:Instrumentation will have no effect")
def test_a_new_sqlalchemy_major_keeps_normal_tracing_checks(monkeypatch):
    from hailhq.core import db

    original_version = db.version
    monkeypatch.setattr(
        db,
        "version",
        lambda package: (
            "3.0.0" if package == "sqlalchemy" else original_version(package)
        ),
    )
    calls = []
    monkeypatch.setattr(
        db.logfire, "instrument_sqlalchemy", lambda **kw: calls.append(kw)
    )
    db._instrument_sqlalchemy()
    assert calls == [{}]
