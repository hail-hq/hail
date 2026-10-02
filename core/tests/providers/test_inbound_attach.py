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
    await telnyx.detach_inbound_number("1293384261075731499", "+14155550100")
    assert fake.request.await_args.kwargs == {"json": {"connection_id": ""}}


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

    async def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "GET":
            return {"data": [{"id": "did-9", "attributes": {"number": "351300509184"}}]}
        return {}

    monkeypatch.setattr(didww, "_request", fake_request)
    # No stored id: looked up by number.
    await didww.attach_inbound_number(None, "+351300509184")
    assert calls[0][:2] == ("GET", "/dids")
    method, path, kwargs = calls[1]
    assert (method, path) == ("PATCH", "/dids/did-9")
    assert kwargs["json"]["data"]["relationships"]["voice_in_trunk"]["data"] == {
        "type": "voice_in_trunks",
        "id": "trunk-1",
    }
    # Detach nulls the relationship.
    await didww.detach_inbound_number("did-9", "+351300509184")
    assert (
        calls[-1][2]["json"]["data"]["relationships"]["voice_in_trunk"]["data"] is None
    )


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

    async def fake_request(method, path, **kwargs):
        return {"data": []}

    monkeypatch.setattr(didww, "_request", fake_request)
    await didww.detach_inbound_number(None, "+351300509184")  # no raise
