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


@pytest.fixture()
def inbound_trunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_in_tx")
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "ST_in_dw")


def test_inbound_trunk_per_carrier(inbound_trunks: None) -> None:
    assert carrier_routing.inbound_trunk("twilio") == "ST_in_tw"
    assert carrier_routing.inbound_trunk("telnyx") == "ST_in_tx"
    assert carrier_routing.inbound_trunk("didww") == "ST_in_dw"


def test_missing_inbound_trunk_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "")
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "")
    with pytest.raises(ValueError, match="LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID"):
        carrier_routing.inbound_trunk("twilio")


def test_carrier_for_inbound_trunk(
    inbound_trunks: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert carrier_routing.carrier_for_inbound_trunk("ST_in_tw") == "twilio"
    assert carrier_routing.carrier_for_inbound_trunk("ST_in_tx") == "telnyx"
    assert carrier_routing.carrier_for_inbound_trunk("ST_in_dw") == "didww"
    with pytest.raises(ValueError, match="No carrier"):
        carrier_routing.carrier_for_inbound_trunk("ST_other")
    with pytest.raises(ValueError, match="No carrier"):
        carrier_routing.carrier_for_inbound_trunk("")
    # An unconfigured carrier is skipped, not a crash.
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "")
    assert carrier_routing.carrier_for_inbound_trunk("ST_in_tx") == "telnyx"


def test_every_carrier_has_inbound_hooks() -> None:
    for entry in carrier_routing.CARRIERS.values():
        assert callable(entry.inbound_trunk)
        assert callable(entry.attach_inbound)
        assert callable(entry.detach_inbound)
