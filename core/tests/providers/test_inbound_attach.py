"""Carrier-side inbound attach/detach for each carrier, with the HTTP mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from hailhq.core.config import settings
from hailhq.core.providers.voice import didww, telnyx, twilio
from hailhq.core.providers.voice.base import CarrierNotConfigured, CarrierRequestError
from twilio.base.exceptions import TwilioRestException


def _twilio_exc(status: int) -> TwilioRestException:
    return TwilioRestException(status, "https://x", msg="x")


@pytest.fixture()
def twilio_trunk(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(settings, "twilio_sip_trunk_sid", "TK1")
    client = MagicMock()
    monkeypatch.setattr(twilio, "_order_client", lambda: client)
    return client.trunking.v1.trunks.return_value


async def test_twilio_attach_creates_when_absent(twilio_trunk: MagicMock) -> None:
    twilio_trunk.phone_numbers.return_value.fetch.side_effect = _twilio_exc(404)
    await twilio.attach_inbound_number("PN1", "+14155550100")
    twilio_trunk.phone_numbers.create.assert_called_once_with(phone_number_sid="PN1")


async def test_twilio_attach_skips_when_present(twilio_trunk: MagicMock) -> None:
    await twilio.attach_inbound_number("PN1", "+14155550100")
    twilio_trunk.phone_numbers.create.assert_not_called()


async def test_twilio_detach_tolerates_404(twilio_trunk: MagicMock) -> None:
    twilio_trunk.phone_numbers.return_value.delete.side_effect = _twilio_exc(404)
    await twilio.detach_inbound_number("PN1", "+14155550100")
    twilio_trunk.phone_numbers.return_value.delete.side_effect = _twilio_exc(500)
    with pytest.raises(CarrierRequestError):
        await twilio.detach_inbound_number("PN1", "+14155550100")


async def test_twilio_requires_trunk_sid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "twilio_sip_trunk_sid", "")
    with pytest.raises(CarrierNotConfigured, match="TWILIO_SIP_TRUNK_SID"):
        await twilio.attach_inbound_number("PN1", "+14155550100")


async def test_telnyx_attach_patches_voice_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "telnyx_connection_id", "conn-1")
    fake = MagicMock()
    fake.request = AsyncMock(return_value={})
    monkeypatch.setattr(telnyx, "_connection_client", lambda: fake)
    await telnyx.attach_inbound_number("1293384261075731499", "+14155550100")
    fake.request.assert_awaited_once_with(
        "PATCH",
        "/phone_numbers/1293384261075731499/voice",
        json={"connection_id": "conn-1"},
    )
    fake.request.reset_mock()
    await telnyx.detach_inbound_number("1293384261075731499", "+14155550100")
    fake.request.assert_not_awaited()


async def test_telnyx_requires_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "telnyx_api_key", "key")
    monkeypatch.setattr(settings, "telnyx_connection_id", "")
    with pytest.raises(CarrierNotConfigured, match="TELNYX_CONNECTION_ID"):
        await telnyx.attach_inbound_number("1", "+14155550100")


async def test_didww_attach_sets_voice_in_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    calls: list[tuple] = []
    client = MagicMock()

    def fake_get(path, params=None, **kwargs):
        calls.append(("GET", path, params))
        if path == "shared_capacity_groups":
            return {
                "data": [
                    {"id": "grp-0", "attributes": {"metered_channels_count": 0}},
                    {"id": "grp-n", "attributes": {"metered_channels_count": None}},
                    {"id": "grp-1", "attributes": {"metered_channels_count": 100}},
                ]
            }
        if path == "dids/did-9":
            return {"data": {"id": "did-9", "relationships": {}}}
        return {"data": [{"id": "did-9", "attributes": {"number": "351300509184"}}]}

    def fake_patch(path, body, **kwargs):
        calls.append(("PATCH", path, body))
        return {}

    client.get.side_effect = fake_get
    client.patch.side_effect = fake_patch
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    # No stored id: looked up by number.
    await didww.attach_inbound_number(None, "+351300509184")
    assert calls[0][:2] == ("GET", "dids")
    method, path, body = calls[-1]
    assert (method, path) == ("PATCH", "dids/did-9")
    assert body["data"]["relationships"]["voice_in_trunk"]["data"] == {
        "type": "voice_in_trunks",
        "id": "trunk-1",
    }
    # The 0-channel plan takes calls only with capacity: the first group
    # with pay-per-minute channels.
    assert body["data"]["relationships"]["shared_capacity_group"]["data"] == {
        "type": "shared_capacity_groups",
        "id": "grp-1",
    }
    # Detach nulls the trunk only.
    await didww.detach_inbound_number("did-9", "+351300509184")
    relationships = calls[-1][2]["data"]["relationships"]
    assert relationships["voice_in_trunk"]["data"] is None
    assert "shared_capacity_group" not in relationships


async def test_didww_attach_needs_a_pay_per_minute_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No capacity group: the number could not take a call, so routing fails
    as a setup problem instead of looking done."""
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    client = MagicMock()

    def fake_get(path, params=None, **kwargs):
        if path == "shared_capacity_groups":
            return {"data": [{"id": "g", "attributes": {"metered_channels_count": 0}}]}
        if path == "dids/did-9":
            return {"data": {"id": "did-9", "relationships": {}}}
        return {"data": [{"id": "did-9", "attributes": {"number": "351300509184"}}]}

    client.get.side_effect = fake_get
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    with pytest.raises(CarrierNotConfigured, match="pay-per-minute"):
        await didww.attach_inbound_number(None, "+351300509184")
    client.patch.assert_not_called()


async def test_didww_requires_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "")
    with pytest.raises(CarrierNotConfigured, match="DIDWW_VOICE_IN_TRUNK_ID"):
        await didww.attach_inbound_number("did-1", "+351300509184")


async def test_didww_detach_does_not_swallow_a_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "")
    with pytest.raises(CarrierNotConfigured, match="DIDWW_API_KEY"):
        await didww.detach_inbound_number(None, "+351300509184")


async def test_didww_detach_tolerates_a_released_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "key")
    client = MagicMock()
    client.get.return_value = {"data": []}
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    await didww.detach_inbound_number(None, "+351300509184")  # no raise
    client.patch.assert_not_called()


async def test_didww_attach_to_a_missing_did_is_a_request_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DID that is not on the account is a failed request (a 502 for the
    customer), not "the server is not configured for inbound calls"."""
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    client = MagicMock()
    client.get.return_value = {"data": []}
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    with pytest.raises(CarrierRequestError) as exc:
        await didww.attach_inbound_number(None, "+351300509184")
    assert not isinstance(exc.value, CarrierNotConfigured)
    assert exc.value.status == 404
    client.patch.assert_not_called()


async def test_didww_attach_keeps_a_group_the_did_already_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator may have put the DID in a group with paid shared channels:
    attaching only sets the trunk. DIDWW returns the relationship only when
    it is included."""
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    client = MagicMock()
    seen: list = []

    def fake_get(path, params=None, **kwargs):
        seen.append((path, params))
        assert path != "shared_capacity_groups"
        ref = {"type": "shared_capacity_groups", "id": "paid"}
        return {
            "data": {
                "id": "did-9",
                "relationships": {"shared_capacity_group": {"data": ref}},
            },
            "included": [{**ref, "attributes": {"shared_channels_count": 2}}],
        }

    client.get.side_effect = fake_get
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    await didww.attach_inbound_number("did-9", "+351300509184")
    assert seen == [("dids/did-9", {"include": "shared_capacity_group"})]
    body = client.patch.call_args[0][1]
    assert "shared_capacity_group" not in body["data"]["relationships"]
    assert body["data"]["relationships"]["voice_in_trunk"]["data"]["id"] == "trunk-1"


async def test_didww_attach_moves_a_did_out_of_an_empty_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group with no channels cannot carry a call: the DID moves to the
    pay-per-minute group."""
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    client = MagicMock()

    def fake_get(path, params=None, **kwargs):
        if path == "shared_capacity_groups":
            return {
                "data": [{"id": "grp-1", "attributes": {"metered_channels_count": 100}}]
            }
        ref = {"type": "shared_capacity_groups", "id": "empty"}
        return {
            "data": {
                "id": "did-9",
                "relationships": {"shared_capacity_group": {"data": ref}},
            },
            "included": [
                {
                    **ref,
                    "attributes": {
                        "metered_channels_count": 0,
                        "shared_channels_count": 0,
                    },
                }
            ],
        }

    client.get.side_effect = fake_get
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    await didww.attach_inbound_number("did-9", "+351300509184")
    body = client.patch.call_args[0][1]
    assert (
        body["data"]["relationships"]["shared_capacity_group"]["data"]["id"] == "grp-1"
    )


def _sip_trunk(**attrs) -> dict:
    return {
        "data": {
            "id": "trunk-1",
            "type": "voice_in_trunks",
            "attributes": {
                "configuration": {"type": "sip_configurations", "attributes": attrs}
            },
        }
    }


async def test_didww_trunk_auth_keeps_the_rest_of_the_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    stored = {
        "host": "example.sip.livekit.cloud",
        "port": 5060,
        "transport_protocol_id": 2,
        "auth_enabled": False,
        "incoming_auth_username": "server-made",
        "incoming_auth_password": "server-made",
    }
    client = MagicMock()
    patches: list[dict] = []

    def fake_patch(path, body, **kwargs):
        assert path == "voice_in_trunks/trunk-1"
        patches.append(body)
        stored.update(body["data"]["attributes"]["configuration"]["attributes"])
        return {}

    client.get.side_effect = lambda path, **kw: _sip_trunk(**stored)
    client.patch.side_effect = fake_patch
    monkeypatch.setattr(didww, "didww_client", lambda: client)

    await didww.set_voice_in_trunk_auth("hail-didww", "s3cret")

    sent = patches[0]["data"]["attributes"]["configuration"]["attributes"]
    assert sent["auth_enabled"] is True
    assert sent["auth_user"] == "hail-didww"
    assert sent["auth_password"] == "s3cret"
    # The whole configuration goes back, without the server-made fields.
    assert sent["host"] == "example.sip.livekit.cloud"
    assert sent["transport_protocol_id"] == 2
    assert "incoming_auth_username" not in sent


async def test_didww_trunk_auth_fails_when_the_trunk_lost_its_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    reads = iter(
        [
            _sip_trunk(host="example.sip.livekit.cloud", port=5060),
            _sip_trunk(auth_enabled=True, auth_user="hail-didww", host=None),
        ]
    )
    client = MagicMock()
    client.get.side_effect = lambda path, **kw: next(reads)
    monkeypatch.setattr(didww, "didww_client", lambda: client)
    with pytest.raises(CarrierNotConfigured, match="host, port or transport"):
        await didww.set_voice_in_trunk_auth("hail-didww", "s3cret")


async def test_didww_trunk_check_reads_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """sip_trunks.py apply runs this before any change: a read, no write."""
    monkeypatch.setattr(settings, "didww_api_key", "key")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    client = MagicMock()
    client.get.side_effect = lambda path, **kw: _sip_trunk(
        host="example.sip.livekit.cloud", port=5060, incoming_auth_username="x"
    )
    monkeypatch.setattr(didww, "didww_client", lambda: client)

    current = await didww.check_voice_in_trunk("hail-didww", "s3cret")

    assert current == {"host": "example.sip.livekit.cloud", "port": 5060}
    client.patch.assert_not_called()

    client.get.side_effect = lambda path, **kw: {
        "data": {
            "id": "trunk-1",
            "attributes": {"configuration": {"type": "pstn_configurations"}},
        }
    }
    with pytest.raises(CarrierNotConfigured, match="not a SIP trunk"):
        await didww.check_voice_in_trunk("hail-didww", "s3cret")
    client.patch.assert_not_called()


async def test_didww_trunk_auth_needs_a_trunk_and_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "")
    with pytest.raises(CarrierNotConfigured, match="DIDWW_VOICE_IN_TRUNK_ID"):
        await didww.set_voice_in_trunk_auth("hail-didww", "s3cret")
    monkeypatch.setattr(settings, "didww_voice_in_trunk_id", "trunk-1")
    with pytest.raises(CarrierNotConfigured, match="password"):
        await didww.set_voice_in_trunk_auth("hail-didww", "")
    with pytest.raises(CarrierNotConfigured, match="password"):
        await didww.check_voice_in_trunk("hail-didww", "")


async def test_twilio_origination_urls_are_read_and_only_the_url_changes(
    twilio_trunk: MagicMock,
) -> None:
    url = MagicMock(
        sid="OU1", sip_url="sip:lk.example.test", priority=10, weight=20, enabled=True
    )
    twilio_trunk.origination_urls.list.return_value = [url]
    assert await twilio.origination_urls() == [
        twilio.OriginationUrl("OU1", "sip:lk.example.test", 10, 20, True)
    ]
    await twilio.set_origination_sip_url("OU1", "sip:lk.example.test?X-A=b")
    twilio_trunk.origination_urls.assert_called_once_with("OU1")
    twilio_trunk.origination_urls.return_value.update.assert_called_once_with(
        sip_url="sip:lk.example.test?X-A=b"
    )


async def test_twilio_origination_url_errors_are_carrier_errors(
    twilio_trunk: MagicMock,
) -> None:
    twilio_trunk.origination_urls.list.side_effect = _twilio_exc(401)
    with pytest.raises(CarrierRequestError):
        await twilio.origination_urls()
    twilio_trunk.origination_urls.return_value.update.side_effect = _twilio_exc(404)
    with pytest.raises(CarrierRequestError):
        await twilio.set_origination_sip_url("OU1", "sip:x")
