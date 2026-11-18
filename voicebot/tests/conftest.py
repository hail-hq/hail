"""Pytest fixtures for the voicebot service."""

from __future__ import annotations

from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_schemas,
    database_url,
    db,
    fresh_db_pools,
    session_factory,
)
