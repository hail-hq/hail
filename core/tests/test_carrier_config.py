import pytest
from hailhq.core.config import Settings


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_legacy_twilio_trunk_alias_and_canonical_precedence(monkeypatch, direction):
    canonical = f"LIVEKIT_TWILIO_SIP_{direction.upper()}_TRUNK_ID"
    legacy = f"LIVEKIT_SIP_{direction.upper()}_TRUNK_ID"
    monkeypatch.delenv(canonical, raising=False)
    monkeypatch.setenv(legacy, "ST_legacy")
    config = Settings(_env_file=None)
    assert getattr(config, canonical.lower()) == "ST_legacy"
    monkeypatch.setenv(canonical, "ST_twilio")
    monkeypatch.setenv("LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID", "ST_telnyx")
    config = Settings(_env_file=None)
    assert getattr(config, canonical.lower()) == "ST_twilio"
    assert config.livekit_telnyx_sip_outbound_trunk_id == "ST_telnyx"
