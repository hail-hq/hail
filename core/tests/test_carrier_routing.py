import pytest
from hailhq.core import carrier_routing
from hailhq.core.config import settings


def test_twilio_uses_twilio_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", "ST_twilio")
    assert carrier_routing.voice_route("twilio") == "ST_twilio"


def test_didww_uses_didww_trunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_sip_outbound_trunk_id", "ST_twilio")
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "ST_didww")
    assert carrier_routing.voice_route("didww") == "ST_didww"


def test_didww_without_trunk_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_didww_sip_outbound_trunk_id", "")
    with pytest.raises(ValueError, match="DIDWW"):
        carrier_routing.voice_route("didww")


def test_unknown_carrier_is_an_error() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        carrier_routing.voice_route("vonage")
