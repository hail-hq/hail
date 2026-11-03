"""The handover line check: did anyone on the contact's side speak, and
was it a machine?"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.voicebot import handover_watch
from livekit import rtc
from livekit.agents.vad import VADEventType
from livekit.agents.voice.amd import AMDCategory

from ._fakes import FakeLLM


def _frame(samples=320):
    return rtc.AudioFrame(
        data=bytes([1, 0]) * samples,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=samples,
    )


class _AudioStream:
    """``rtc.AudioStream``: one 20 ms frame every few ms, forever."""

    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.001)
        return SimpleNamespace(frame=_frame())

    async def aclose(self) -> None:
        self.closed = True


class _VADStream:
    """Emits one scripted event per pushed frame, then INFERENCE_DONE ticks."""

    def __init__(self, script: list[VADEventType]) -> None:
        self._script = list(script)
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    def push_frame(self, _frame) -> None:
        kind = self._script.pop(0) if self._script else VADEventType.INFERENCE_DONE
        self._queue.put_nowait(SimpleNamespace(type=kind))

    def end_input(self) -> None:
        self._queue.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def leg(monkeypatch):
    audio = _AudioStream()
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=object())),
    )
    monkeypatch.setattr(handover_watch.rtc.AudioStream, "from_track", lambda **_: audio)
    return audio


def _vad(script):
    stream = _VADStream(script)
    return SimpleNamespace(stream=lambda: stream), stream


async def _speaks(vad, seconds, **kwargs) -> bool:
    line = handover_watch.ContactLine(object(), "human-x", vad, **kwargs)
    try:
        return await line.speaks(seconds)
    finally:
        await line.aclose()


async def test_speech_counts(leg) -> None:
    vad, stream = _vad(
        [VADEventType.INFERENCE_DONE] * 3 + [VADEventType.START_OF_SPEECH]
    )
    assert await _speaks(vad, 2.0)
    assert leg.closed and stream.closed


async def test_silence_until_the_deadline(leg) -> None:
    vad, stream = _vad([])
    assert not await _speaks(vad, 0.05)
    assert leg.closed and stream.closed


async def test_no_audio_track_is_silent(monkeypatch) -> None:
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=None)),
    )
    vad, _ = _vad([VADEventType.START_OF_SPEECH])
    assert not await asyncio.wait_for(_speaks(vad, 1.0), timeout=0.5)


async def test_deadline_includes_waiting_for_the_track(monkeypatch) -> None:
    """A leg that never publishes audio still ends the check on time."""

    async def never(**_):
        await asyncio.sleep(3600)

    monkeypatch.setattr(handover_watch, "wait_for_track_publication", never)
    vad, _ = _vad([])
    assert not await asyncio.wait_for(_speaks(vad, 0.05), timeout=1)


# --- machine check -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_checks(monkeypatch):
    monkeypatch.setattr(handover_watch, "CHECK_EVERY_SECONDS", 0.01)
    monkeypatch.setattr(handover_watch, "PAUSE_WAIT_SECONDS", 0.01)


def _check(*answers):
    """A greeting check that returns ``answers`` in order, then the last."""
    calls: list[int] = []

    async def check(audio):
        calls.append(audio.samples_per_channel)
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return check, calls


_SPEECH = (
    [VADEventType.START_OF_SPEECH]
    + [VADEventType.INFERENCE_DONE] * 5
    + [VADEventType.END_OF_SPEECH]
)


async def _verdict(check, script=None):
    vad, _ = _vad(script or _SPEECH)
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    try:
        assert await line.speaks(1.0)
        machine = await asyncio.wait_for(line.machine(), timeout=2)
        return machine, line.verdict()
    finally:
        await line.aclose()


@pytest.mark.parametrize(
    "category", [AMDCategory.MACHINE_VM, AMDCategory.MACHINE_UNAVAILABLE]
)
async def test_mailbox_verdict(leg, category) -> None:
    check, calls = _check(("You have reached Sam", category))
    machine, verdict = await _verdict(check)
    assert machine is True and len(calls) == 1
    assert verdict["category"] == category.value
    assert verdict["transcript"] == "You have reached Sam"
    assert verdict["reason"] == "greeting_check"
    assert verdict["delay"] >= 0


@pytest.mark.parametrize(
    "category",
    [AMDCategory.HUMAN, AMDCategory.MACHINE_IVR, AMDCategory.UNCERTAIN],
)
async def test_person_menu_or_unsure_keeps_checking_then_connects(
    leg, category
) -> None:
    """Only a mailbox verdict acts. A person's verdict is not final (a
    greeting can open like a person), and a phone menu stays connected so
    the caller can press keys: the check runs all its tries."""
    check, calls = _check(("Hello?", category))
    machine, verdict = await _verdict(check)
    assert machine is False
    assert len(calls) == handover_watch.MAX_CHECKS
    assert verdict["category"] == category.value
    assert verdict["reason"] == "window_ended"


async def test_person_then_mailbox_drops(leg) -> None:
    """'Hi, this is Sam' sounds like a person; the rest of the greeting
    shows the mailbox. Each check sends the longer greeting."""
    check, calls = _check(
        ("Hi, this is Sam", AMDCategory.HUMAN),
        ("Hi, this is Sam, leave a message", AMDCategory.MACHINE_VM),
    )
    machine, verdict = await _verdict(check)
    assert machine is True and len(calls) == 2 and calls[1] > calls[0]
    assert verdict["transcript"] == "Hi, this is Sam, leave a message"


async def test_checks_are_capped(leg) -> None:
    check, calls = _check(("Hi, this is Sam", AMDCategory.UNCERTAIN))
    machine, _ = await _verdict(check)
    assert machine is False and len(calls) == handover_watch.MAX_CHECKS


async def test_no_new_audio_ends_the_check(monkeypatch, leg) -> None:
    """The audio cap is full: the check stops instead of waking again."""
    monkeypatch.setattr(handover_watch, "MAX_AUDIO_SECONDS", 0.05)
    check, calls = _check(("Hi", AMDCategory.UNCERTAIN))

    async def slow(audio):
        await asyncio.sleep(0.05)  # the cap fills while it checks
        return await check(audio)

    machine, _ = await _verdict(slow)
    assert machine is False and len(calls) == 1


async def test_silence_before_speech_is_not_sent(leg) -> None:
    """Two seconds of silence, then speech: the check gets about one second
    of audio from before the speech (the preroll), not the whole wait."""
    check, calls = _check(("Leave a message", AMDCategory.MACHINE_VM))
    machine, _ = await _verdict(
        check,
        [VADEventType.INFERENCE_DONE] * 100
        + [VADEventType.START_OF_SPEECH, VADEventType.END_OF_SPEECH],
    )
    assert machine is True
    preroll = int(handover_watch.PREROLL_SECONDS * 16000)
    assert preroll <= calls[0] < preroll + 16000


async def test_closed_mid_check_connects(leg) -> None:
    started = asyncio.Event()

    async def check(_audio):
        started.set()
        await asyncio.sleep(3600)

    vad, _ = _vad(list(_SPEECH))
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    assert await line.speaks(1.0)
    machine = asyncio.ensure_future(line.machine())
    await asyncio.wait_for(started.wait(), timeout=1)
    await line.aclose()
    assert await machine is False
    assert line.verdict()["category"] is None
    assert line.verdict()["reason"] == "window_ended"


async def test_failed_check_connects(leg) -> None:
    check, calls = _check(RuntimeError("provider down"))
    machine, verdict = await _verdict(check)
    assert machine is False and len(calls) == 1
    assert verdict["category"] is None and verdict["reason"] == "detection_failed"


async def test_no_check_connects(leg) -> None:
    machine, verdict = await _verdict(None)
    assert machine is False
    assert verdict == {
        "category": None,
        "transcript": None,
        "reason": "detection_unavailable",
        "delay": None,
    }


async def test_no_verdict_without_speech(leg) -> None:
    check, calls = _check(("x", AMDCategory.MACHINE_VM))
    vad, _ = _vad([])
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    assert not await line.speaks(0.05)
    assert await line.machine() is False
    assert line.verdict() is None and calls == []
    await line.aclose()


@pytest.mark.parametrize("where", ["track", "vad"])
async def test_audio_error_connects(monkeypatch, leg, where) -> None:
    """A read or VAD error is not a silent line: connect, as before."""
    if where == "track":
        monkeypatch.setattr(
            handover_watch,
            "wait_for_track_publication",
            AsyncMock(side_effect=RuntimeError("no track")),
        )
        vad = SimpleNamespace(stream=lambda: None)
    else:

        def _broken():
            raise RuntimeError("vad down")

        vad = SimpleNamespace(stream=_broken)
    line = handover_watch.ContactLine(object(), "human-x", vad)
    assert await asyncio.wait_for(line.speaks(5.0), timeout=1) is True
    assert line.verdict() is None
    await line.aclose()


# --- building the check --------------------------------------------------------


def _layer(provider, **kw):
    return handover_watch.ResolvedLayer(
        provider=provider,
        api_key=kw.get("api_key"),
        params=kw.get("params", {}),
        fallback_enabled=kw.get("fallback_enabled", False),
    )


@pytest.fixture
def deepgram(monkeypatch):
    monkeypatch.setattr(handover_watch.settings, "deepgram_api_key", "dg-house")
    monkeypatch.setattr(handover_watch.settings, "deepgram_model", "nova-3")
    built: list[tuple] = []

    def _stt(model, api_key):
        built.append((model, api_key))
        return SimpleNamespace(model=model, aclose=AsyncMock())

    monkeypatch.setattr(handover_watch, "recheck_stt", _stt)
    return built


def test_stt_follows_the_org_choice(deepgram) -> None:
    assert handover_watch._build_stt({}) is not None
    assert handover_watch._build_stt({"stt": _layer("deepgram", api_key="dg-org")})
    # The org's model when it takes a recording; Hail's model otherwise.
    org = {"model": "nova-3-medical"}
    assert handover_watch._build_stt(
        {"stt": _layer("deepgram", api_key="dg-org", params=org)}
    )
    streaming_only = {"model": "flux-general-en"}
    assert handover_watch._build_stt(
        {"stt": _layer("deepgram", api_key="dg-org", params=streaming_only)}
    )
    # Another provider: its audio never goes to a provider it did not pick.
    assert handover_watch._build_stt({"stt": _layer("speechmatics")}) is None
    # Hail's key only when the org allows fallback.
    assert handover_watch._build_stt({"stt": _layer("deepgram")}) is None
    assert handover_watch._build_stt({"stt": _layer("deepgram", fallback_enabled=True)})
    assert deepgram == [
        ("nova-3", "dg-house"),
        ("nova-3", "dg-org"),
        ("nova-3-medical", "dg-org"),
        ("nova-3", "dg-org"),
        ("nova-3", "dg-house"),
    ]


def test_no_deepgram_key_means_no_check(monkeypatch) -> None:
    monkeypatch.setattr(handover_watch.settings, "deepgram_api_key", "")
    check, owned = handover_watch.build_greeting_check(SimpleNamespace(llm=None))
    assert check is None and owned == []


def test_no_classifier_means_no_check(monkeypatch, deepgram) -> None:
    monkeypatch.setattr(handover_watch, "build_classifier", lambda *a, **k: (None, []))
    check, owned = handover_watch.build_greeting_check(SimpleNamespace(llm=None))
    assert check is None and len(owned) == 1  # the transcriber, to close


async def test_check_uses_the_amd_prompt_for_a_partial_greeting(
    monkeypatch, deepgram
) -> None:
    llm = FakeLLM()
    monkeypatch.setattr(handover_watch, "build_classifier", lambda *a, **k: (llm, []))
    seen: dict = {}

    async def _tc(audio, *, stt, llm, prompt, note):
        seen.update(stt=stt, llm=llm, prompt=prompt, note=note)
        return "Leave a message", AMDCategory.MACHINE_VM

    monkeypatch.setattr(handover_watch, "transcribe_and_classify", _tc)
    check, owned = handover_watch.build_greeting_check(SimpleNamespace(llm=None))
    assert check is not None
    assert await check(_frame()) == ("Leave a message", AMDCategory.MACHINE_VM)
    assert seen["llm"] is llm and seen["stt"] is owned[0]
    assert seen["prompt"] == handover_watch.GREETING_CLASSIFICATION_PROMPT
    assert seen["note"] == handover_watch.PARTIAL_NOTE
