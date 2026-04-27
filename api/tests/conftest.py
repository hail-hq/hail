"""Test fixtures for the API service.

Mirrors ``core/conftest.py``'s testcontainers-vs-external-Postgres
pattern. The fixture is reimplemented here (rather than imported)
because ``core/`` is a uv workspace member without an importable
``conftest`` module, and the core ``session`` fixture is sync.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncIterator

# uv's editable .pth files put each package's ``hailhq/`` subdir directly
# on sys.path, so ``import hailhq`` only resolves to the cwd's match and
# ``hailhq.core`` fails when tests run from the api workspace. Adding
# ``core/`` (the parent of ``core/hailhq``) gives the namespace package
# its second match. Underlying packaging fix is tracked separately.
_CORE_DIR = Path(__file__).resolve().parents[2] / "core"
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.api.db import to_async_url  # noqa: E402


@pytest.fixture(scope="session")
def database_url() -> AsyncIterator[str]:
    """Resolve the test database URL.

    Honors a pre-set ``DATABASE_URL`` (e.g. CI's Postgres service
    container); falls back to a session-scoped testcontainers Postgres
    so local dev works with no extra setup.
    """
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        yield env_url
        return

    # Lazy import so runs against an external DATABASE_URL don't need
    # the testcontainers / docker stack installed or running.
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "psycopg")


@pytest.fixture()
async def async_session(database_url: str) -> AsyncIterator[AsyncSession]:
    """A per-test ``AsyncSession`` against a freshly recreated schema."""
    from hailhq.core.models import Base

    engine = create_async_engine(to_async_url(database_url))

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session

    await engine.dispose()
