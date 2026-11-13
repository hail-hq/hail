"""Audio filtering stays on the agent input; it never republishes a track."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from hailhq.core.config import settings
from hailhq.voicebot import audio
from hailhq.voicebot.audio import room_options
from livekit import rtc


@pytest.mark.parametrize(
    "url,expected",
    [("wss://test.livekit.cloud", True), ("wss://livekit.example.org", False)],
)
def test_worker_prewarms_cloud_filter_before_calls(monkeypatch, url, expected):
    from hailhq.voicebot import agent

    monkeypatch.setattr(settings, "livekit_url", url)
    load_filter = Mock()
    monkeypatch.setattr(audio, "import_module", load_filter)
    vad = object()
    monkeypatch.setattr(agent.silero.VAD, "load", lambda: vad)
    proc = SimpleNamespace(userdata={})
    agent.prewarm(proc)
    assert proc.userdata["vad"] is vad
    if expected:
        load_filter.assert_called_once_with("livekit.plugins.noise_cancellation")
    else:
        load_filter.assert_not_called()


def test_cloud_sip_uses_telephony_voice_isolation(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    selector = room_options().audio_input.noise_cancellation
    option = selector(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
        )
    )
    assert option is not None
    assert "inb.bvc" in option.options["modelPath"]


def test_self_hosted_transport_does_not_request_cloud_filter(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://livekit.example.org")
    assert room_options().audio_input.noise_cancellation is None


def test_cloud_web_audio_uses_voice_isolation(monkeypatch):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    selector = room_options().audio_input.noise_cancellation
    option = selector(
        SimpleNamespace(
            participant=SimpleNamespace(
                kind=rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
            )
        )
    )
    assert option is not None
    assert "inb.bvc" not in option.options["modelPath"]


@pytest.mark.parametrize(
    "choice,module,sip_factory,web_factory",
    [
        ("bvc", "livekit.plugins.noise_cancellation", "BVCTelephony", "BVC"),
        (
            "krisp_viva",
            "livekit.plugins.krisp",
            "voice_isolation_telephony",
            "voice_isolation",
        ),
    ],
)
def test_configured_cloud_filter_uses_supported_participant_selector(
    monkeypatch, choice, module, sip_factory, web_factory
):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "hail_audio_filter", choice)
    factories = SimpleNamespace(
        **{sip_factory: Mock(return_value="sip"), web_factory: Mock(return_value="web")}
    )
    imported = Mock(return_value=factories)
    monkeypatch.setattr(audio, "import_module", imported)
    selector = room_options().audio_input.noise_cancellation
    for kind, expected in [
        (rtc.ParticipantKind.PARTICIPANT_KIND_SIP, "sip"),
        (rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, "web"),
    ]:
        assert (
            selector(SimpleNamespace(participant=SimpleNamespace(kind=kind)))
            == expected
        )
    imported.assert_called_once_with(module)


@pytest.mark.parametrize(
    "choice,model", [("voice_focus_s", "small"), ("voice_focus_l", "large")]
)
def test_voice_focus_uses_documented_model(monkeypatch, choice, model):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "hail_audio_filter", choice)
    factory = Mock(return_value=object())
    plugin = SimpleNamespace(
        audio_enhancement=factory,
        EnhancerModel=SimpleNamespace(QUAIL_VF_S="small", QUAIL_VF_L="large"),
    )
    monkeypatch.setattr(audio, "import_module", Mock(return_value=plugin))
    selector = room_options().audio_input.noise_cancellation
    selector(
        SimpleNamespace(
            participant=SimpleNamespace(kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
        )
    )
    factory.assert_called_once_with(model=model)


@pytest.mark.parametrize("value", ["", "unknown"])
def test_unknown_filter_falls_back_to_bvc(monkeypatch, value):
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(settings, "hail_audio_filter", value)
    imported = Mock(return_value=SimpleNamespace(BVCTelephony=Mock(return_value="sip")))
    monkeypatch.setattr(audio, "import_module", imported)
    selector = room_options().audio_input.noise_cancellation
    kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
    assert selector(SimpleNamespace(participant=SimpleNamespace(kind=kind))) == "sip"
    imported.assert_called_once_with("livekit.plugins.noise_cancellation")
