"""Shared pytest fixtures for the Hail workspace.

Import the fixtures you need from here in each package's ``conftest.py``::

    from hailhq.core.testing.fixtures import async_session, database_url  # noqa: F401
"""

from __future__ import annotations

import os
from typing import AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.core import db as core_db
from hailhq.core.db import to_async_url


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


@pytest.fixture()
async def async_session(
    database_url: str,  # noqa: F811 (re-used as a fixture parameter name)
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncSession]:
    """A per-test ``AsyncSession`` against a freshly recreated schema.

    Also installs the test sessionmaker as ``hailhq.core.db._sessionmaker``
    so production ``session_scope()`` callers (auth bookkeeping, audit-log
    writes, voicebot event writes) talk to the test database without any
    FastAPI dep override.
    """
    from hailhq.core.models import Base

    engine = create_async_engine(to_async_url(database_url))

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(core_db, "_sessionmaker", sessionmaker)

    async with sessionmaker() as session:
        yield session

    await engine.dispose()
