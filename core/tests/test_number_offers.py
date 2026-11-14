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


async def test_discover_passes_each_carrier_its_own_approvals(monkeypatch):
    from hailhq.core.carrier_offer import Approval

    calls = {name: AsyncMock(return_value=[]) for name in ("twilio", "telnyx", "didww")}
    monkeypatch.setattr(f"{VOICE}.twilio.twilio_offers", calls["twilio"])
    monkeypatch.setattr(f"{VOICE}.telnyx.telnyx_offers", calls["telnyx"])
    monkeypatch.setattr(f"{VOICE}.didww.didww_offers", calls["didww"])
    person = Approval(row_id=uuid4(), subject_type="person", verification_id="BU1")
    await number_offers.discover_offers(
        uuid4(), "GB", "mobile", ["voice"], approvals={"twilio": [person]}
    )
    assert calls["twilio"].await_args.kwargs["approvals"] == [person]
    assert calls["telnyx"].await_args.kwargs["approvals"] == []


def _stub_carriers(monkeypatch):
    calls = {name: AsyncMock(return_value=[]) for name in ("twilio", "telnyx", "didww")}
    monkeypatch.setattr(f"{VOICE}.twilio.twilio_offers", calls["twilio"])
    monkeypatch.setattr(f"{VOICE}.telnyx.telnyx_offers", calls["telnyx"])
    monkeypatch.setattr(f"{VOICE}.didww.didww_offers", calls["didww"])
    return calls


async def test_toll_free_sms_quote_asks_only_carriers_hail_can_verify(monkeypatch):
    # Hail verifies US/CA toll-free texting with Twilio only. A Telnyx
    # toll-free number bought for SMS could never text.
    for country in ("US", "CA"):
        calls = _stub_carriers(monkeypatch)
        _, unavailable = await number_offers.discover_offers(
            uuid4(), country, "toll_free", ["voice", "sms"]
        )
        assert unavailable == []
        calls["twilio"].assert_awaited_once()
        calls["telnyx"].assert_not_awaited()
        calls["didww"].assert_not_awaited()


async def test_toll_free_calls_only_quote_asks_every_carrier(monkeypatch):
    calls = _stub_carriers(monkeypatch)
    await number_offers.discover_offers(uuid4(), "US", "toll_free", ["voice"])
    for name in ("twilio", "telnyx", "didww"):
        calls[name].assert_awaited_once()


async def test_non_toll_free_sms_quote_asks_every_carrier(monkeypatch):
    calls = _stub_carriers(monkeypatch)
    await number_offers.discover_offers(uuid4(), "US", "local", ["voice", "sms"])
    for name in ("twilio", "telnyx", "didww"):
        calls[name].assert_awaited_once()


def test_toll_free_sms_ok():
    ok = number_offers.toll_free_sms_ok
    assert ok("twilio", "US", "toll_free", ["sms"])
    assert not ok("telnyx", "US", "toll_free", ["sms"])
    assert not ok("telnyx", "CA", "toll_free", ["voice", "sms"])
    assert ok("telnyx", "US", "toll_free", ["voice"])
    assert ok("telnyx", "US", "local", ["sms"])
    # Outside the US and Canada the toll-free verification does not apply.
    assert ok("telnyx", "GB", "toll_free", ["sms"])


def test_by_request_toll_free_sms_skips_carriers_hail_cannot_verify(
    tmp_path, monkeypatch
):
    import json

    from hailhq.core import telephony_catalog

    row = {
        "country_code": "US",
        "number_type": "toll_free",
        "usd_per_month": "1.00",
        "voice": True,
        "sms": True,
        "mms": False,
        "by_request": True,
    }
    (tmp_path / "twilio.json").write_text(json.dumps({"numbers": []}))
    (tmp_path / "telnyx.json").write_text(json.dumps({"numbers": [row]}))
    (tmp_path / "didww.json").write_text(json.dumps({"numbers": []}))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    try:
        assert (
            number_offers.by_request_offers("US", "toll_free", ["voice", "sms"]) == []
        )
        (asked,) = number_offers.by_request_offers("US", "toll_free", ["voice"])
        assert asked.provider == "telnyx"
    finally:
        telephony_catalog._load.cache_clear()
