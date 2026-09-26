import pytest
from hailhq.core import carrier_routing
from hailhq.core.config import settings


@pytest.fixture(autouse=True)
def trunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_twilio")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_outbound_trunk_id", "ST_telnyx")
    monkeypatch.setattr(settings, "telnyx_sip_username", "hail-sip")
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "ST_didww")


def test_each_carrier_uses_its_own_trunk() -> None:
    assert carrier_routing.voice_route("twilio") == ("ST_twilio", {})
    assert carrier_routing.voice_route("telnyx") == (
        "ST_telnyx",
        {"X-Telnyx-Username": "hail-sip"},
    )
    assert carrier_routing.voice_route("didww") == ("ST_didww", {})


@pytest.mark.parametrize(
    ("provider", "setting"),
    [
        ("twilio", "livekit_twilio_sip_outbound_trunk_id"),
        ("telnyx", "livekit_telnyx_sip_outbound_trunk_id"),
        ("telnyx", "telnyx_sip_username"),
        ("didww", "livekit_didww_sip_outbound_trunk_id"),
    ],
)
def test_missing_trunk_is_an_error(
    monkeypatch: pytest.MonkeyPatch, provider: str, setting: str
) -> None:
    monkeypatch.setattr(settings, setting, "")
    with pytest.raises(ValueError, match=setting.upper()):
        carrier_routing.voice_route(provider)


def test_unknown_carrier_is_an_error() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        carrier_routing.voice_route("vonage")


def test_didww_sells_no_sms_through_hail() -> None:
    with pytest.raises(ValueError, match="cannot send SMS"):
        carrier_routing.sms_route("didww", object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="cannot send SMS"):
        carrier_routing.sms_status_path("didww")
    assert carrier_routing.sms_status_path("twilio") == "sms/status"
    assert carrier_routing.sms_status_path("telnyx") == "sms/telnyx"
