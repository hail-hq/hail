"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlsplit

from hailhq.core.db import engine_options, to_async_url, to_sync_url


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


def test_to_async_url_drops_pgbouncer_client_flag():
    # Better Auth URLs use this flag. asyncpg.connect rejects it.
    rewritten = to_async_url(
        "postgres://u:p@h/db?pgbouncer=true&sslmode=require&prepared_statement_cache_size=0"
    )
    assert parse_qs(urlsplit(rewritten).query) == {
        "ssl": ["require"],
        "prepared_statement_cache_size": ["0"],
    }


def test_to_async_url_no_query_unchanged_apart_from_scheme():
    assert to_async_url("postgres://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"


def test_to_sync_url_keeps_sslmode():
    # psycopg understands sslmode natively — don't touch it.
    rewritten = to_sync_url("postgres://u:p@h/db?sslmode=require")
    assert rewritten.startswith("postgresql+psycopg://")
    assert "sslmode=require" in rewritten


def test_to_sync_url_drops_params_psycopg_rejects():
    rewritten = to_sync_url(
        "postgres://u:p@h/db?pgbouncer=true&sslmode=require&prepared_statement_cache_size=0"
    )
    assert parse_qs(urlsplit(rewritten).query) == {"sslmode": ["require"]}


def test_to_sync_url_leaves_other_params_byte_for_byte():
    url = "postgres://u:p@h/db?sslmode=require&options=-c%20geqo%3Doff"
    assert to_sync_url(url) == url.replace("postgres://", "postgresql+psycopg://")


def test_to_sync_url_drop_leaves_the_kept_params_byte_for_byte():
    url = "postgres://u:p@h/db?pgbouncer=true&sslrootcert=/etc/ssl/ca.pem&options=a=b"
    assert to_sync_url(url) == (
        "postgresql+psycopg://u:p@h/db?sslrootcert=/etc/ssl/ca.pem&options=a=b"
    )


def test_a_url_with_no_host_stays_valid():
    url = "postgres:///hail?host=/var/run/postgresql&pgbouncer=true"
    assert to_sync_url(url) == ("postgresql+psycopg:///hail?host=/var/run/postgresql")
    assert to_async_url(url).startswith("postgresql+asyncpg:///hail?host=")


def test_a_rewritten_query_keeps_the_text_after_a_hash():
    assert to_async_url("postgres://u:p@h/db?sslmode=require#tail") == (
        "postgresql+asyncpg://u:p@h/db?ssl=require#tail"
    )
    assert to_sync_url("postgres://u:p@h/db?pgbouncer=true&sslmode=require#tail") == (
        "postgresql+psycopg://u:p@h/db?sslmode=require#tail"
    )


def test_pgbouncer_flag_turns_off_prepared_statements():
    assert engine_options("postgres://u:p@h/db?pgbouncer=true&sslmode=require") == {
        "connect_args": {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    }


def test_urls_without_the_flag_keep_prepared_statements():
    assert engine_options("postgres://u:p@h/db?sslmode=require") == {}
    assert engine_options("postgres://u:p@h/db?pgbouncer=false") == {}
    assert engine_options("postgres://u:p@h/db") == {}


def test_the_shared_engine_uses_the_url_options(monkeypatch):
    from hailhq.core import db

    calls = []
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: False)
    monkeypatch.setattr(
        db.settings, "database_url", "postgres://u:p@h/db?pgbouncer=true"
    )
    monkeypatch.setattr(
        db, "create_async_engine", lambda url, **options: calls.append((url, options))
    )

    db._ensure_initialized()

    assert calls == [
        (
            "postgresql+asyncpg://u:p@h/db",
            {
                "connect_args": {
                    "statement_cache_size": 0,
                    "prepared_statement_cache_size": 0,
                }
            },
        )
    ]


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
