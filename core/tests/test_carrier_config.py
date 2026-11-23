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
    ("shared", "twilio"),
    [("", ""), ("ST_shared", ""), ("ST_shared", "ST_shared")],
    ids=["no_trunk", "falls_back_to_shared", "equals_shared"],
)
def test_sip_secret_needs_the_twilio_trunk(monkeypatch, shared, twilio):
    """Only the Twilio trunk maps the header: on the shared trunk (or none),
    the secret would refuse every Twilio call. The value is never printed."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_TRUNK_ID", shared)
    monkeypatch.setenv("LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID", twilio)
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", SECRET)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "HAIL_SIP_INBOUND_SECRET needs LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID" in str(
        exc.value
    )
    assert SECRET not in str(exc.value)


@pytest.mark.parametrize("twilio", ["", "ST_shared"])
def test_empty_sip_secret_needs_no_twilio_trunk(monkeypatch, twilio):
    """Self-host default: no secret, any trunk setup starts."""
    monkeypatch.setenv("LIVEKIT_SIP_INBOUND_TRUNK_ID", "ST_shared")
    monkeypatch.setenv("LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID", twilio)
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", "")
    assert Settings(_env_file=None).hail_sip_inbound_secret == ""


@pytest.mark.parametrize(
    "value", ["short-but-ok", "has a space-near-miss-secret", "amp&er=sand" * 3]
)
def test_sip_secret_unfit_for_a_uri_fails_at_startup(
    monkeypatch, own_twilio_trunk, value
):
    """The error names the setting and never prints the value."""
    monkeypatch.setenv("HAIL_SIP_INBOUND_SECRET", value)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    assert "HAIL_SIP_INBOUND_SECRET must be empty or 20 or more" in str(exc.value)
    assert value not in str(exc.value)
