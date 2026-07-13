"""Shared pytest fixtures for the Hail workspace.

Import the fixtures you need from here in each package's ``conftest.py``::

    from hailhq.core.testing.fixtures import async_session, database_url, db, session_factory  # noqa: F401
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, AsyncIterator, Iterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.core import db as core_db
from hailhq.core.db import to_async_url
from hailhq.core.models import OrganizationMember, User


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
async def db(
    database_url: str,  # noqa: F811 (re-used as a fixture parameter name)
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncEngine, None]:
    """Fresh async engine with recreated schema; patches the global sessionmaker.

    Base fixture for all async DB tests. ``async_session`` and
    ``session_factory`` compose on top of this.
    """
    from hailhq.core.models import Base

    engine = create_async_engine(to_async_url(database_url))

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(
        core_db, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False)
    )

    yield engine

    await engine.dispose()


@pytest.fixture()
def session_factory(db: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Async session factory backed by the test engine."""
    return async_sessionmaker(db, expire_on_commit=False)


@pytest.fixture()
async def async_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A per-test ``AsyncSession`` against a freshly recreated schema.

    Also installs the test sessionmaker as ``hailhq.core.db._sessionmaker``
    so production ``session_scope()`` callers (auth bookkeeping, audit-log
    writes, voicebot event writes) talk to the test database without any
    FastAPI dep override.

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
    session.add(User(id=uid, name=name, email=email, phone_number=phone))
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
