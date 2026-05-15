"""Tests for ``DATABASE_URL`` coercion helpers in :mod:`hailhq.core.db`."""

from __future__ import annotations

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
