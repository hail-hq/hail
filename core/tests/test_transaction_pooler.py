"""The runtime engine works through a transaction-mode pooler.

A transaction-mode pooler (PgBouncer ``pool_mode = transaction``, Supabase's
shared pooler on its transaction port) hands each transaction to any backend
and does not track prepared statements. Runs only when
``HAIL_TEST_TRANSACTION_POOLER_URL`` points at such a pooler; the schema is
never touched, so it is safe on an empty database.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import text

POOLER_URL = os.environ.get("HAIL_TEST_TRANSACTION_POOLER_URL")

pytestmark = pytest.mark.skipif(
    not POOLER_URL, reason="HAIL_TEST_TRANSACTION_POOLER_URL is unset"
)


async def test_many_sessions_share_pooler_backends(monkeypatch):
    from hailhq.core import db

    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_sessionmaker", None)
    monkeypatch.setattr(db.settings, "database_url", POOLER_URL)
    monkeypatch.setattr(db.settings, "database_transaction_pooler", True)
    monkeypatch.setattr(db, "telemetry_enabled", lambda: False)

    def expected(n: int) -> list[tuple[str, object]]:
        # A statement run on the wrong backend fails or returns another
        # query's result, so every answer is checked.
        return [
            ("SELECT CAST(:n AS integer) + 1", n + 1),
            ("SELECT length(CAST(:s AS text))", len(str(n))),
            ("SELECT CAST(:n AS integer) * 2", n * 2),
            ("SELECT pg_advisory_xact_lock(hashtextextended(:s, 0)) IS NULL", False),
        ]

    async def worker(i: int) -> None:
        for round_ in range(25):
            n = i * 100 + round_
            async with db.session_scope() as session:
                for query, want in expected(n):
                    got = await session.execute(text(query), {"n": n, "s": str(n)})
                    assert got.scalar() == want, query
                await session.commit()

    try:
        await asyncio.gather(*(worker(i) for i in range(40)))
    finally:
        await db.dispose_engine()
