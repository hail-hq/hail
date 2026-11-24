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
from hailhq.core import config
from hailhq.core.livekit import LiveKitClient
from livekit import api

CALL_ID = UUID("11111111-2222-3333-4444-555555555555")
CONN_ID = UUID("66666666-7777-8888-9999-000000000000")


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> LiveKitClient:
    """A ``LiveKitClient`` with its underlying ``LiveKitAPI`` mocked out."""
    monkeypatch.setattr(config.settings, "livekit_url", "wss://example.livekit.cloud")
    monkeypatch.setattr(config.settings, "livekit_api_key", "API_test")
    monkeypatch.setattr(config.settings, "livekit_api_secret", "secret_test")

    fake_api = MagicMock(spec=api.LiveKitAPI)
    fake_api.room = MagicMock()
    fake_api.room.create_room = AsyncMock()
    fake_api.room.delete_room = AsyncMock()
    fake_api.agent_dispatch = MagicMock()
    fake_api.agent_dispatch.create_dispatch = AsyncMock()
    fake_api.agent_dispatch.delete_dispatch = AsyncMock()
    fake_api.sip = MagicMock()
    fake_api.sip.create_sip_participant = AsyncMock()
    fake_api.sip.create_inbound_trunk = AsyncMock()
    fake_api.sip.create_outbound_trunk = AsyncMock()
    fake_api.sip.update_inbound_trunk_fields = AsyncMock()
    fake_api.sip.update_outbound_trunk_fields = AsyncMock()
    fake_api.sip.delete_trunk = AsyncMock()
    fake_api.sip.list_dispatch_rule = AsyncMock()
    fake_api.sip.update_dispatch_rule = AsyncMock()
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


async def test_wait_for_agent_readiness_requires_an_explicit_audio_ready_signal(client):
    from hailhq.core.livekit import VOICE_READY_ATTRIBUTE

    client._lkapi.room.list_participants = AsyncMock(
        side_effect=[
            api.ListParticipantsResponse(
                participants=[api.ParticipantInfo(identity="agent")]
            ),
            api.ListParticipantsResponse(
                participants=[
                    api.ParticipantInfo(
                        identity="agent", attributes={VOICE_READY_ATTRIBUTE: "true"}
                    )
                ]
            ),
        ]
    )
    await client.wait_for_agent_ready("room", poll_interval=0)
    assert client._lkapi.room.list_participants.await_count == 2


async def test_agent_readiness_has_a_deadline(client):
    client._lkapi.room.list_participants = AsyncMock(
        return_value=api.ListParticipantsResponse()
    )
    with pytest.raises(TimeoutError):
        await client.wait_for_agent_ready("room", timeout=0.01, poll_interval=0.001)


async def test_agent_readiness_stops_when_the_worker_gave_up(client):
    from hailhq.core.livekit import WorkerGaveUp

    client._lkapi.room.list_participants = AsyncMock(
        return_value=api.ListParticipantsResponse()
    )

    async def gave_up() -> bool:
        return True

    with pytest.raises(WorkerGaveUp):
        await client.wait_for_agent_ready("room", poll_interval=0, gave_up=gave_up)
    assert client._lkapi.room.list_participants.await_count == 1


async def test_dispatch_agent_serializes_metadata_and_returns_id(
    client: LiveKitClient,
) -> None:
    fake_dispatch = api.AgentDispatch(
        id="AD_abc123",
        agent_name="hail-voicebot",
        room=f"hail-{CALL_ID}",
    )
    client._lkapi.agent_dispatch.create_dispatch.return_value = fake_dispatch

    # org_name is None on every lookup failure, pin that None survives the
    # real json.dumps round-trip the dispatch channel performs (the api
    # tests mock dispatch_agent, so this is the only place it runs).
    metadata = {
        "call_id": str(CALL_ID),
        "system_prompt": "be brief",
        "org_name": None,
    }
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
    # The proto field is a string, we must serialize the dict ourselves.
    assert req.metadata == json.dumps(metadata)
    assert json.loads(req.metadata)["org_name"] is None


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


async def test_delete_dispatch_delegates_to_underlying_api(
    client: LiveKitClient,
) -> None:
    await client.delete_dispatch("AD_abc123", f"hail-{CALL_ID}")
    client._lkapi.agent_dispatch.delete_dispatch.assert_awaited_once_with(
        "AD_abc123",
        f"hail-{CALL_ID}",
    )


async def test_delete_room_wraps_delete_room_request(client: LiveKitClient) -> None:
    await client.delete_room(f"hail-{CALL_ID}")
    client._lkapi.room.delete_room.assert_awaited_once()
    (req,), _ = client._lkapi.room.delete_room.call_args
    assert isinstance(req, api.DeleteRoomRequest)
    assert req.room == f"hail-{CALL_ID}"


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


async def test_telnyx_header_is_sent_on_sip_invite(client: LiveKitClient) -> None:
    await client.create_sip_participant(
        room_name="test",
        to_e164="+14155559999",
        from_e164="+14155551234",
        sip_trunk_id="ST_telnyx",
        participant_identity="caller",
        headers={"X-Telnyx-Username": "hail-sip"},
    )
    (req,), _ = client._lkapi.sip.create_sip_participant.call_args
    assert dict(req.headers) == {"X-Telnyx-Username": "hail-sip"}


async def test_add_and_remove_inbound_number(client: LiveKitClient) -> None:
    from livekit.protocol.models import ListUpdate

    client._lkapi.sip.update_inbound_trunk_fields = AsyncMock()
    await client.add_inbound_number("ST_in", "+14155550100")
    args, kwargs = client._lkapi.sip.update_inbound_trunk_fields.await_args
    assert args == ("ST_in",)
    assert isinstance(kwargs["numbers"], ListUpdate)
    assert list(kwargs["numbers"].add) == ["+14155550100"]

    await client.remove_inbound_number("ST_in", "+14155550100")
    _, kwargs = client._lkapi.sip.update_inbound_trunk_fields.await_args
    assert list(kwargs["numbers"].remove) == ["+14155550100"]


async def test_create_pbx_inbound_trunk_lists_only_the_placeholder(client):
    await client.create_pbx_inbound_trunk(CONN_ID, "hail-ab12", "p" * 32)
    (req,), _ = client._lkapi.sip.create_inbound_trunk.call_args
    assert isinstance(req, api.CreateSIPInboundTrunkRequest)
    assert req.trunk.name == f"pbx-{CONN_ID}"
    assert list(req.trunk.numbers) == [f"pbx-{CONN_ID}"]
    assert req.trunk.auth_username == "hail-ab12"


async def test_create_pbx_outbound_trunk_dials_the_pbx(client):
    await client.create_pbx_outbound_trunk(
        CONN_ID, "pbx.example.com", 5060, "tls", None, None
    )
    (req,), _ = client._lkapi.sip.create_outbound_trunk.call_args
    assert req.trunk.name == f"pbx-{CONN_ID}-out"
    assert req.trunk.address == "pbx.example.com:5060"
    assert req.trunk.transport == api.SIPTransport.SIP_TRANSPORT_TLS
    assert list(req.trunk.numbers) == ["*"]
    assert req.trunk.auth_username == ""


@pytest.mark.parametrize(
    "name,enum",
    [
        ("auto", api.SIPTransport.SIP_TRANSPORT_AUTO),
        ("udp", api.SIPTransport.SIP_TRANSPORT_UDP),
        ("tcp", api.SIPTransport.SIP_TRANSPORT_TCP),
        ("tls", api.SIPTransport.SIP_TRANSPORT_TLS),
    ],
)
async def test_transport_map(client, name, enum):
    await client.create_pbx_outbound_trunk(
        CONN_ID, "pbx.example.com", 5060, name, None, None
    )
    (req,), _ = client._lkapi.sip.create_outbound_trunk.call_args
    assert req.trunk.transport == enum


async def test_set_pbx_inbound_credentials_changes_only_auth(client):
    await client.set_pbx_inbound_credentials("ST_1", "hail-ab12", "q" * 32)
    client._lkapi.sip.update_inbound_trunk_fields.assert_awaited_once_with(
        "ST_1", auth_username="hail-ab12", auth_password="q" * 32
    )


async def test_update_pbx_outbound_trunk_sets_address_and_auth(client):
    await client.update_pbx_outbound_trunk(
        "ST_2", "pbx.example.com", 5061, "tcp", "pbxuser", "pw"
    )
    client._lkapi.sip.update_outbound_trunk_fields.assert_awaited_once_with(
        "ST_2",
        address="pbx.example.com:5061",
        transport=api.SIPTransport.SIP_TRANSPORT_TCP,
        auth_username="pbxuser",
        auth_password="pw",
    )


async def test_remove_inbound_number_refuses_placeholder(client):
    with pytest.raises(ValueError):
        await client.remove_inbound_number("ST_1", f"pbx-{CONN_ID}")
    client._lkapi.sip.update_inbound_trunk_fields.assert_not_awaited()


async def test_delete_sip_trunk_treats_404_as_done(client):
    client._lkapi.sip.delete_trunk.side_effect = api.TwirpError(
        "not_found", "gone", status=404
    )
    assert await client.delete_sip_trunk("ST_1") is None
    client._lkapi.sip.delete_trunk.assert_awaited_once()


async def test_delete_sip_trunk_raises_other_errors(client):
    client._lkapi.sip.delete_trunk.side_effect = api.TwirpError(
        "internal", "boom", status=500
    )
    with pytest.raises(api.TwirpError):
        await client.delete_sip_trunk("ST_1")


async def test_create_sip_participant_can_wait_for_answer(client):
    await client.create_sip_participant(
        "r",
        "+14155550100",
        "+14155550199",
        "ST_1",
        "check-1",
        wait_until_answered=True,
        ringing_timeout_seconds=30,
    )
    (req,), _ = client._lkapi.sip.create_sip_participant.call_args
    assert req.wait_until_answered is True
    assert req.ringing_timeout.seconds == 30


async def test_clear_dispatch_rule_trunks_updates_rule_with_no_trunks(client):
    rule = api.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_1", name="hail-inbound", trunk_ids=["ST_a", "ST_b"]
    )
    other = api.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_2", name="other", trunk_ids=["ST_c"]
    )
    client._lkapi.sip.list_dispatch_rule.return_value = api.ListSIPDispatchRuleResponse(
        items=[other, rule]
    )
    client._lkapi.sip.update_dispatch_rule.return_value = api.SIPDispatchRuleInfo(
        sip_dispatch_rule_id="SDR_1", name="hail-inbound"
    )
    assert await client.clear_dispatch_rule_trunks("hail-inbound") == ("SDR_1", [])
    (rule_id, sent), _ = client._lkapi.sip.update_dispatch_rule.call_args
    assert rule_id == "SDR_1"
    assert list(sent.trunk_ids) == [] and sent.name == "hail-inbound"
    client._lkapi.sip.update_dispatch_rule.assert_awaited_once()


async def test_clear_dispatch_rule_trunks_unknown_name_is_none(client):
    client._lkapi.sip.list_dispatch_rule.return_value = api.ListSIPDispatchRuleResponse(
        items=[]
    )
    assert await client.clear_dispatch_rule_trunks("hail-inbound") is None
    client._lkapi.sip.update_dispatch_rule.assert_not_awaited()
