from unittest.mock import AsyncMock
from uuid import uuid4

from hailhq.core import number_offers


async def test_discover_asks_didww(monkeypatch):
    calls = {}
    for name in ("twilio", "telnyx", "didww"):
        calls[name] = AsyncMock(return_value=[])
    monkeypatch.setattr(number_offers, "twilio_offers", calls["twilio"])
    monkeypatch.setattr(number_offers, "telnyx_offers", calls["telnyx"])
    monkeypatch.setattr(number_offers, "didww_offers", calls["didww"])
    offers, unavailable = await number_offers.discover_offers(
        uuid4(), "PT", "national", ["voice"]
    )
    assert offers == [] and unavailable == []
    calls["didww"].assert_awaited_once()
    assert number_offers.PROVIDERS == ("twilio", "telnyx", "didww")


async def test_discover_reports_didww_outage(monkeypatch):
    monkeypatch.setattr(number_offers, "twilio_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(number_offers, "telnyx_offers", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        number_offers, "didww_offers", AsyncMock(side_effect=RuntimeError("down"))
    )
    _, unavailable = await number_offers.discover_offers(
        uuid4(), "PT", "national", ["voice"]
    )
    assert unavailable == ["didww"]
