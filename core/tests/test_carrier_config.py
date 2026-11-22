import pytest
from hailhq.core.config import Settings
from pydantic import ValidationError


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


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_blank_canonical_trunk_does_not_hide_a_legacy_value(monkeypatch, direction):
    canonical = f"LIVEKIT_TWILIO_SIP_{direction.upper()}_TRUNK_ID"
    monkeypatch.setenv(canonical, "")
    monkeypatch.setenv(f"LIVEKIT_SIP_{direction.upper()}_TRUNK_ID", "ST_legacy")
    assert getattr(Settings(_env_file=None), canonical.lower()) == "ST_legacy"


def test_shared_inbound_trunk_feeds_every_carrier(monkeypatch):
    for name in (
        "LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID",
        "LIVEKIT_TELNYX_SIP_INBOUND_TRUNK_ID",
        "LIVEKIT_DIDWW_SIP_INBOUND_TRUNK_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_TRUNK_ID", "ST_shared")
    config = Settings(_env_file=None)
    assert config.livekit_twilio_sip_inbound_trunk_id == "ST_shared"
    assert config.livekit_telnyx_sip_inbound_trunk_id == "ST_shared"
    assert config.livekit_didww_sip_inbound_trunk_id == "ST_shared"
    monkeypatch.setenv("LIVEKIT_DIDWW_SIP_INBOUND_TRUNK_ID", "ST_dw")
    assert Settings(_env_file=None).livekit_didww_sip_inbound_trunk_id == "ST_dw"


@pytest.mark.parametrize("value", ["", "a" * 20, "Ab0_-" * 8])
def test_sip_secret_accepts_empty_or_uri_safe(monkeypatch, value):
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", value)
    assert Settings(_env_file=None).hail_sip_inbound_secret == value


@pytest.mark.parametrize(
    "value", ["short-but-ok", "has a space-near-miss-secret", "amp&er=sand" * 3]
)
def test_sip_secret_unfit_for_a_uri_fails_at_startup(monkeypatch, value):
    """The error names the setting and never prints the value."""
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", value)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "HAIL_SIP_INBOUND_SECRET must be empty or 20 or more" in str(exc.value)
    assert value not in str(exc.value)
