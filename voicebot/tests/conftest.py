"""Pytest fixtures for the voicebot service."""

from __future__ import annotations

import pytest

from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_schemas,
    database_url,
    db,
    fresh_db_pools,
    session_factory,
)


class _NoSpeakingGuard:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def attach(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_speaking_gate_in_entrypoint_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entrypoint tests run on mocked sessions: the gate would open a real
    audio stream on a mock participant. tests/test_speaking_gate.py tests the
    gate itself, from its own module."""
    from hailhq.voicebot import agent  # after the fixtures above set the env

    monkeypatch.setattr(agent, "SpeakingGuard", _NoSpeakingGuard)
