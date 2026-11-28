"""Pytest fixtures for the voicebot service."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from hailhq.core import voice_catalog
from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_schemas,
    database_url,
    db,
    fresh_db_pools,
    session_factory,
)


@pytest.fixture(autouse=True)
def no_live_voice_lists(monkeypatch: pytest.MonkeyPatch):
    """Tests never load a provider's real voice list; each starts empty."""
    voice_catalog.reset_preset_voice_cache()
    monkeypatch.setattr(
        voice_catalog, "_fetch_preset_voices", AsyncMock(return_value=[])
    )
    yield
    voice_catalog.reset_preset_voice_cache()
