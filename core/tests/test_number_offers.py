from unittest.mock import AsyncMock
from uuid import uuid4

from hailhq.core import number_offers

VOICE = "hailhq.core.providers.voice"


async def test_discover_asks_didww(monkeypatch):
    calls = {}
    for name in ("twilio", "telnyx", "didww"):
        calls[name] = AsyncMock(return_value=[])
    monkeypatch.setattr(f"{VOICE}.twilio.twilio_offers", calls["twilio"])
    monkeypatch.setattr(f"{VOICE}.telnyx.telnyx_offers", calls["telnyx"])
    monkeypatch.setattr(f"{VOICE}.didww.didww_offers", calls["didww"])
    offers, unavailable = await number_offers.discover_offers(
        uuid4(), "PT", "national", ["voice"]
    )
    assert offers == [] and unavailable == []
    calls["didww"].assert_awaited_once()
    assert number_offers.PROVIDERS == ("twilio", "telnyx", "didww")


async def test_discover_reports_didww_outage(monkeypatch):
    monkeypatch.setattr(f"{VOICE}.twilio.twilio_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(f"{VOICE}.telnyx.telnyx_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        f"{VOICE}.didww.didww_offers", AsyncMock(side_effect=RuntimeError("down"))
    )
    _, unavailable = await number_offers.discover_offers(
        uuid4(), "PT", "national", ["voice"]
    )
    assert unavailable == ["didww"]
