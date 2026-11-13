"""Shared pytest fixtures for the Hail workspace.

Import the fixtures you need from here in each package's ``conftest.py``::

    from hailhq.core.testing.fixtures import async_session, database_schemas, database_url, db, session_factory  # noqa: F401
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from datetime import datetime, timezone

import pytest
from hailhq.core import db as core_db
from hailhq.core.db import to_async_url
from hailhq.core.models import OrganizationMember, User
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Resolve the test database URL.

    Honors a pre-set ``DATABASE_URL`` (e.g. CI's Postgres service
    container); otherwise spins up a session-scoped testcontainers
    Postgres so local dev works with no extra setup.
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "psycopg")


@pytest.fixture(scope="session")
def database_schemas() -> set[str]:
    """Databases initialized for this pytest session (no cross-run cache)."""
    return set()


def prepare_database(
    conn: Connection,
    database_url: str,
    database_schemas: set[str],
    *,
    recreate: bool = False,
) -> None:
    """Keep real commits/concurrent sessions, but avoid per-test schema DDL.

    Tests that change schema must use ``pytest.mark.recreate_schema``. They
    get a fresh schema and invalidate it for the following test as well.
    Migration fixtures that bypass this helper must invalidate the cache.
    """
    from hailhq.core.models import Base

    if recreate or database_url not in database_schemas:
        database_schemas.discard(database_url)
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    else:
        tables = ", ".join(
            conn.dialect.identifier_preparer.format_table(table)
            for table in Base.metadata.sorted_tables
        )
        conn.exec_driver_sql(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
    if not recreate:
        database_schemas.add(database_url)


@pytest.fixture()
async def db(
    database_url: str,
    database_schemas: set[str],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncGenerator[AsyncEngine, None]:
    """Fresh async engine with empty tables; patches the global sessionmaker.

    Base fixture for all async DB tests. ``async_session`` and
    ``session_factory`` compose on top of this.
    """
    engine = create_async_engine(to_async_url(database_url))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                prepare_database,
                database_url,
                database_schemas,
                recreate=request.node.get_closest_marker("recreate_schema") is not None,
            )

        monkeypatch.setattr(
            core_db, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
        )
        monkeypatch.setattr(
            core_db,
            "_independent_sessionmaker",
            async_sessionmaker(engine, expire_on_commit=False),
        )
        monkeypatch.setattr(
            core_db,
            "_background_sessionmaker",
            async_sessionmaker(engine, expire_on_commit=False),
        )

        yield engine
    finally:
        # Background transactions must release locks before the next reset.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await engine.dispose()


@pytest.fixture()
def session_factory(db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Async session factory backed by the test engine."""
    return async_sessionmaker(db, expire_on_commit=False)


@pytest.fixture()
async def async_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A per-test ``AsyncSession`` against empty tables.

    Also installs the test sessionmaker as ``hailhq.core.db._sessionmaker``
    and the independent and background sessionmakers, so production
    ``session_scope()`` and ``independent_session_scope()`` callers (audit, usage, idempotency,
    voicebot event writes) talk to the test database without any FastAPI
    dep override.

    The ``db`` fixture handles the schema setup and sessionmaker patch.
    """
    async with session_factory() as session:
        yield session


async def seed_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    email: str,
    phone: str | None = None,
    role: str = "member",
) -> uuid.UUID:
    """Insert a users + members row for contacts-union tests.

    Shared by api/tests/test_contacts_api.py, api/tests/test_contacts_core.py,
    and voicebot/tests/test_agent.py — previously three near-identical local
    copies. ``created_at`` is stamped explicitly (rather than left to a
    server default) so seeded members sort predictably relative to
    manual-contact rows created moments later in the same test.
    """
    uid = uuid.uuid4()
    session.add(
        User(
            id=uid,
            name=name,
            email=email,
            phone_number=phone,
            created_at=datetime.now(timezone.utc),
        )
    )
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            organization_id=org_id,
            user_id=uid,
            role=role,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return uid
