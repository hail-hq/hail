"""Pytest fixtures for core."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from hailhq.core import voice_catalog


@pytest.fixture(autouse=True)
def no_live_voice_lists(monkeypatch: pytest.MonkeyPatch):
    """Tests never load a provider's real voice list; each starts empty."""
    voice_catalog.reset_preset_voice_cache()
    monkeypatch.setattr(
        voice_catalog, "_fetch_preset_voices", AsyncMock(return_value=[])
    )
    yield
    voice_catalog.reset_preset_voice_cache()
