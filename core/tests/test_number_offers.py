from unittest.mock import AsyncMock
from uuid import uuid4

from hailhq.core import number_offers
from hailhq.core.carrier_offer import CarrierOffer

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


def _offer(provider: str, country: str, e164: str) -> CarrierOffer:
    return CarrierOffer(
        provider=provider,
        e164=e164,
        country_code=country,
        number_type="local",
        capabilities=["voice", "sms"],
        monthly_cents=100,
        setup_cents=0,
        currency="USD",
        readiness="ready",
        regulatory_friction="none",
    )


def test_rank_prefers_telnyx_for_us_at_equal_price() -> None:
    ranked = number_offers.rank_offers(
        [_offer("twilio", "US", "+12025550100"), _offer("telnyx", "US", "+12025550101")]
    )
    assert [o.provider for o in ranked] == ["telnyx", "twilio"]


def test_rank_keeps_first_carrier_elsewhere_at_equal_price() -> None:
    ranked = number_offers.rank_offers(
        [
            _offer("telnyx", "GB", "+442079460000"),
            _offer("twilio", "GB", "+442079460001"),
        ]
    )
    assert [o.provider for o in ranked] == ["twilio", "telnyx"]


def test_rank_still_picks_cheaper_over_preferred() -> None:
    cheap = _offer("twilio", "US", "+12025550100").model_copy(
        update={"monthly_cents": 50}
    )
    ranked = number_offers.rank_offers([_offer("telnyx", "US", "+12025550101"), cheap])
    assert ranked[0].provider == "twilio"
