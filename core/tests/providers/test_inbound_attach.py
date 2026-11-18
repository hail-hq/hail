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
