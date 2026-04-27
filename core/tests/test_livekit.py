"""Unit tests for ``LiveKitClient``.

We patch the underlying ``livekit.api.LiveKitAPI`` so we never open a
network connection, but we still assert against the **real** request
types (``CreateRoomRequest`` / ``CreateAgentDispatchRequest`` /
``CreateSIPParticipantRequest``). That way the tests catch SDK API
drift: if LiveKit renames a proto field these tests fail the same way
real usage would.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from livekit import api

from hailhq.core import config
from hailhq.core.livekit import LiveKitClient

CALL_ID = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> LiveKitClient:
    """A ``LiveKitClient`` with its underlying ``LiveKitAPI`` mocked out."""
    monkeypatch.setattr(config.settings, "livekit_url", "wss://example.livekit.cloud")
    monkeypatch.setattr(config.settings, "livekit_api_key", "API_test")
    monkeypatch.setattr(config.settings, "livekit_api_secret", "secret_test")

    fake_api = MagicMock(spec=api.LiveKitAPI)
    fake_api.room = MagicMock()
    fake_api.room.create_room = AsyncMock()
    fake_api.agent_dispatch = MagicMock()
    fake_api.agent_dispatch.create_dispatch = AsyncMock()
    fake_api.sip = MagicMock()
    fake_api.sip.create_sip_participant = AsyncMock()
    fake_api.aclose = AsyncMock()

    with patch("hailhq.core.livekit.api.LiveKitAPI", return_value=fake_api):
        yield LiveKitClient()


async def test_create_room_uses_hail_call_id_naming(client: LiveKitClient) -> None:
    room_name = await client.create_room(CALL_ID)

    assert room_name == f"hail-{CALL_ID}"
    client._lkapi.room.create_room.assert_awaited_once()
    (req,), _ = client._lkapi.room.create_room.call_args
    assert isinstance(req, api.CreateRoomRequest)
    assert req.name == f"hail-{CALL_ID}"


async def test_dispatch_agent_serializes_metadata_and_returns_id(
    client: LiveKitClient,
) -> None:
    fake_dispatch = api.AgentDispatch(
        id="AD_abc123",
        agent_name="hail-voicebot",
        room=f"hail-{CALL_ID}",
    )
    client._lkapi.agent_dispatch.create_dispatch.return_value = fake_dispatch

    metadata = {"call_id": str(CALL_ID), "system_prompt": "be brief"}
    dispatch_id = await client.dispatch_agent(
        room_name=f"hail-{CALL_ID}",
        agent_name="hail-voicebot",
        metadata=metadata,
    )

    assert dispatch_id == "AD_abc123"
    client._lkapi.agent_dispatch.create_dispatch.assert_awaited_once()
    (req,), _ = client._lkapi.agent_dispatch.create_dispatch.call_args
    assert isinstance(req, api.CreateAgentDispatchRequest)
    assert req.room == f"hail-{CALL_ID}"
    assert req.agent_name == "hail-voicebot"
    # The proto field is a string — we must serialize the dict ourselves.
    assert req.metadata == json.dumps(metadata)


async def test_create_sip_participant_maps_to_e164_and_from_e164(
    client: LiveKitClient,
) -> None:
    fake_info = api.SIPParticipantInfo(
        participant_id="PA_xyz",
        participant_identity="caller-7c2a",
        room_name=f"hail-{CALL_ID}",
        sip_call_id="SCL_abc",
    )
    client._lkapi.sip.create_sip_participant.return_value = fake_info

    result = await client.create_sip_participant(
        room_name=f"hail-{CALL_ID}",
        to_e164="+14155559999",
        from_e164="+14155551234",
        sip_trunk_id="ST_trunk1",
        participant_identity="caller-7c2a",
    )

    assert result is fake_info
    client._lkapi.sip.create_sip_participant.assert_awaited_once()
    (req,), _ = client._lkapi.sip.create_sip_participant.call_args
    assert isinstance(req, api.CreateSIPParticipantRequest)
    # sip_call_to is the destination; sip_number is the caller-ID/from
    # (per livekit_sip.proto).
    assert req.sip_call_to == "+14155559999"
    assert req.sip_number == "+14155551234"
    assert req.sip_trunk_id == "ST_trunk1"
    assert req.room_name == f"hail-{CALL_ID}"
    assert req.participant_identity == "caller-7c2a"
    assert req.participant_name == "caller-7c2a"


async def test_aclose_delegates_to_underlying_api(client: LiveKitClient) -> None:
    await client.aclose()
    client._lkapi.aclose.assert_awaited_once()


def test_constructor_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit args, the client pulls from ``settings``."""
    monkeypatch.setattr(config.settings, "livekit_url", "wss://from-settings")
    monkeypatch.setattr(config.settings, "livekit_api_key", "key_from_settings")
    monkeypatch.setattr(config.settings, "livekit_api_secret", "secret_from_settings")

    with patch("hailhq.core.livekit.api.LiveKitAPI") as mock_ctor:
        LiveKitClient()
        mock_ctor.assert_called_once_with(
            "wss://from-settings",
            "key_from_settings",
            "secret_from_settings",
        )


def test_constructor_accepts_explicit_overrides() -> None:
    with patch("hailhq.core.livekit.api.LiveKitAPI") as mock_ctor:
        LiveKitClient(
            url="wss://override",
            api_key="override_key",
            api_secret="override_secret",
        )
        mock_ctor.assert_called_once_with(
            "wss://override",
            "override_key",
            "override_secret",
        )
