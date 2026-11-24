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


@pytest.fixture
def own_twilio_trunk(monkeypatch):
    """A Twilio trunk apart from the shared one: the secret may be set."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_TRUNK_ID", "ST_shared")
    monkeypatch.setenv("LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID", "ST_tw")


@pytest.mark.parametrize("value", ["", "a" * 20, "Ab0_-" * 8])
def test_sip_secret_accepts_empty_or_uri_safe(monkeypatch, own_twilio_trunk, value):
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", value)
    assert Settings(_env_file=None).hail_sip_inbound_secret == value


SECRET = "a" * 32


@pytest.mark.parametrize(
    ("shared", "twilio", "value"),
    [
        ("", "", SECRET),
        ("ST_shared", "", SECRET),
        ("ST_shared", "ST_shared", SECRET),
        ("ST_shared", "ST_tw", "short-but-ok"),
        ("ST_shared", "ST_tw", "has a space-near-miss-secret"),
        ("ST_shared", "ST_tw", "amp&er=sand" * 3),
    ],
    ids=[
        "no_trunk",
        "falls_back_to_shared",
        "equals_shared",
        "short",
        "space",
        "ampersand",
    ],
)
def test_sip_secret_never_stops_startup(monkeypatch, shared, twilio, value):
    """A secret that cannot work turns the check off at call time
    (inbound_calls.py, with an ERROR log): services still start."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_TRUNK_ID", shared)
    monkeypatch.setenv("LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID", twilio)
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", value)
    assert Settings(_env_file=None).hail_sip_inbound_secret == value
