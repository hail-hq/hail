"""Test fixtures for the API service."""

from __future__ import annotations

from typing import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hailhq.api import db as api_db
from hailhq.api.db import to_async_url
from hailhq.core.testing.fixtures import database_url  # noqa: F401


@pytest.fixture()
async def async_session(
    database_url: str,  # noqa: F811 (re-used as a fixture parameter name)
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncSession]:
    """A per-test ``AsyncSession`` against a freshly recreated schema.

    Also installs the test sessionmaker as ``hailhq.api.db._sessionmaker``
    so ``session_scope`` (used by background-stamping in the auth dep)
    talks to the same database without needing FastAPI dep overrides.
    """
    from hailhq.core.models import Base

    engine = create_async_engine(to_async_url(database_url))

    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(api_db, "_sessionmaker", sessionmaker)

    async with sessionmaker() as session:
        yield session

    await engine.dispose()
