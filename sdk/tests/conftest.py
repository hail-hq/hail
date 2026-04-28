"""Shared fixtures for the SDK test suite."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wipe Hail env vars from every test so client-construction is deterministic.

    Tests that want a key inject it explicitly. ``HAIL_API_URL`` defaults to
    the production base in production code; we point at a benign localhost so
    a respx-bypass doesn't accidentally hit the real API.
    """
    monkeypatch.delenv("HAIL_API_KEY", raising=False)
    monkeypatch.delenv("HAIL_API_URL", raising=False)


@pytest.fixture
def base_url() -> str:
    return "https://api.test"


@pytest.fixture
def api_key() -> str:
    return "sk-test"


def make_call_response(
    *,
    call_id: UUID | None = None,
    status: str = "queued",
) -> dict:
    """Server-shaped JSON for a CallResponse."""
    cid = call_id or uuid4()
    return {
        "id": str(cid),
        "organization_id": str(uuid4()),
        "conversation_id": None,
        "from_e164": "+15550001111",
        "to_e164": "+15555550123",
        "direction": "outbound",
        "status": status,
        "end_reason": None,
        "provider_call_sid": None,
        "livekit_room": None,
        "initial_prompt": "test prompt",
        "recording_s3_key": None,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "answered_at": None,
        "ended_at": None,
    }


def make_event(
    *,
    call_id: UUID,
    kind: str = "agent_turn",
    payload: dict | None = None,
    occurred_at: datetime | None = None,
    event_id: UUID | None = None,
) -> dict:
    return {
        "id": str(event_id or uuid4()),
        "call_id": str(call_id),
        "kind": kind,
        "payload": payload if payload is not None else {"text": "hello"},
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
    }
