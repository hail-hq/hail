"""Audio filtering stays on the agent input; it never republishes a track."""

import threading
import time
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


def test_prewarm_does_not_wait_on_a_stuck_voice_list_load(monkeypatch):
    from hailhq.voicebot import agent

    stuck = threading.Event()
    monkeypatch.setattr(agent, "_warm_voice_lists", stuck.wait)
    monkeypatch.setattr(agent, "_VOICE_LIST_WARM_LIMIT", 0.05)
    monkeypatch.setattr(agent.silero.VAD, "load", object)
    monkeypatch.setattr(audio, "import_module", Mock())
    started = time.monotonic()
    try:
        agent.prewarm(SimpleNamespace(userdata={}))
    finally:
        stuck.set()
    assert time.monotonic() - started < 2


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
