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
    monkeypatch.setattr(handover_watch, "FIRST_CHECK_SPEECH_SECONDS", 0.0)


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


async def test_closed_mid_check_is_a_timeout(leg) -> None:
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
    assert line.verdict()["reason"] == "timeout"


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
        "reason": "no_check",
        "delay": None,
        "action": None,
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


class _BrokenStream(_AudioStream):
    """An audio stream whose iterator fails after a few frames."""

    def __init__(self) -> None:
        super().__init__()
        self.sent = 0

    async def __anext__(self):
        self.sent += 1
        if self.sent > 3:
            raise RuntimeError("stream broke")
        return await super().__anext__()


@pytest.mark.parametrize("where", ["iterator", "push_frame"])
async def test_feed_error_connects(monkeypatch, where) -> None:
    """An error inside the frame feed is logged and connects, as before."""
    audio = _BrokenStream() if where == "iterator" else _AudioStream()
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=object())),
    )
    monkeypatch.setattr(handover_watch.rtc.AudioStream, "from_track", lambda **_: audio)
    vad, stream = _vad([])
    if where == "push_frame":

        def _push(_frame):
            raise RuntimeError("vad down")

        stream.push_frame = _push
    line = handover_watch.ContactLine(object(), "human-x", vad)
    assert await asyncio.wait_for(line.speaks(5.0), timeout=1) is True
    await line.aclose()


async def test_first_check_runs_at_the_first_pause(monkeypatch, leg) -> None:
    """A short hello is checked at its pause, not after CHECK_EVERY_SECONDS."""
    monkeypatch.setattr(handover_watch, "CHECK_EVERY_SECONDS", 5.0)
    check, calls = _check(("You have reached Sam", AMDCategory.MACHINE_VM))
    machine, _ = await asyncio.wait_for(_verdict(check), timeout=1)
    assert machine is True and len(calls) == 1


async def test_pause_during_the_gap_counts(monkeypatch, leg) -> None:
    """A pause that comes while the next check waits out its gap is kept:
    the check runs when the gap ends, not after PAUSE_WAIT_SECONDS more."""
    monkeypatch.setattr(handover_watch, "CHECK_EVERY_SECONDS", 0.2)
    monkeypatch.setattr(handover_watch, "PAUSE_WAIT_SECONDS", 5.0)
    check, calls = _check(
        ("Hi, this is Sam", AMDCategory.UNCERTAIN),
        ("Hi, this is Sam, leave a message", AMDCategory.MACHINE_VM),
    )
    # One pause at the hello, one more during the gap, then none.
    script = [VADEventType.START_OF_SPEECH, VADEventType.END_OF_SPEECH]
    script += [VADEventType.INFERENCE_DONE] * 20 + [VADEventType.END_OF_SPEECH]
    machine, _ = await asyncio.wait_for(_verdict(check, script), timeout=2)
    assert machine is True and len(calls) == 2


async def test_no_check_starts_that_cannot_finish_in_the_window(
    monkeypatch, leg
) -> None:
    """A check starts only while it can still end before the bridge decides
    (window minus RECHECK_TIMEOUT_SECONDS)."""
    monkeypatch.setattr(handover_watch, "RECHECK_TIMEOUT_SECONDS", 0.2)
    check, calls = _check(("Hi", AMDCategory.UNCERTAIN))
    vad, _ = _vad(list(_SPEECH))
    line = handover_watch.ContactLine(
        object(), "human-x", vad, check=check, window_seconds=0.2
    )
    assert await line.speaks(1.0)
    assert await asyncio.wait_for(line.machine(), timeout=1) is False
    assert calls == []
    await line.aclose()


async def _line_mid_check(check_result=None):
    """A line whose first check is running (blocked)."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def check(_audio):
        started.set()
        await release.wait()
        return check_result or ("leave a message", AMDCategory.MACHINE_VM)

    vad, _ = _vad(list(_SPEECH))
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    assert await line.speaks(1.0)
    await asyncio.wait_for(started.wait(), timeout=1)
    return line, release


async def test_cancelled_machine_wait_is_a_timeout(leg) -> None:
    """The bridge stops waiting (grace over): the running check is
    cancelled, never lands, and the row says timeout with the last finished
    verdict."""
    line, release = await _line_mid_check()
    waiting = asyncio.ensure_future(line.machine())
    await asyncio.sleep(0.01)
    waiting.cancel()
    await asyncio.gather(waiting, return_exceptions=True)
    release.set()
    await asyncio.sleep(0.01)
    assert not line.is_mailbox
    assert line.verdict()["reason"] == "timeout"
    assert line.verdict()["category"] is None
    await line.aclose()


@pytest.mark.parametrize("how", ["stop", "end"])
async def test_stop_ends_the_check_now(leg, how) -> None:
    """Stopping mid-check cancels it: it never lands, and the reason is
    timeout with the last finished verdict."""
    line, release = await _line_mid_check()
    getattr(line, how)()
    assert await asyncio.wait_for(line.machine(), timeout=1) is False
    release.set()
    assert line.verdict()["reason"] == "timeout" and not line.is_mailbox
    await line.aclose()


async def test_stopped_line_starts_no_check(leg) -> None:
    """Job shutdown stopped the line before its first speech: speech then
    starts no check, and the row says why."""
    check, calls = _check(("leave a message", AMDCategory.MACHINE_VM))
    vad, _ = _vad([VADEventType.INFERENCE_DONE] * 20 + list(_SPEECH))
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    line.end()
    assert await line.speaks(1.0) is False  # end() also ends the wait
    await asyncio.sleep(0.1)  # the speech arrives after the stop
    assert await line.machine() is False and calls == []
    await line.aclose()


async def test_decide_records_what_the_bridge_did(leg) -> None:
    check, _ = _check(("leave a message", AMDCategory.MACHINE_VM))
    vad, _ = _vad(list(_SPEECH))
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    assert await line.speaks(1.0)
    assert await asyncio.wait_for(line.machine(), timeout=1) is True
    line.decide("dropped")
    assert line.verdict()["action"] == "dropped"
    assert line.verdict()["reason"] == "greeting_check"
    # Two fields: what the detector saw, and what the bridge did.
    line.decide("leg_ended")
    assert line.verdict()["reason"] == "greeting_check"
    assert line.verdict()["action"] == "leg_ended"
    await line.aclose()


async def test_feed_error_after_speech_is_recorded(monkeypatch) -> None:
    """The stream breaks after the first speech: connect, and the row says
    audio_error, not window_ended."""
    audio = _BrokenStream()
    monkeypatch.setattr(
        handover_watch,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=object())),
    )
    monkeypatch.setattr(handover_watch.rtc.AudioStream, "from_track", lambda **_: audio)
    vad, _ = _vad([VADEventType.START_OF_SPEECH])
    check, _ = _check(("Hi", AMDCategory.UNCERTAIN))
    line = handover_watch.ContactLine(object(), "human-x", vad, check=check)
    assert await line.speaks(1.0)
    await asyncio.wait_for(line.machine(), timeout=1)
    assert line.verdict()["reason"] == "audio_error"
    await line.aclose()


async def test_pause_before_new_speech_does_not_count(monkeypatch, leg) -> None:
    """A pause, then speech again: the check waits for the next pause
    instead of cutting the new words."""
    monkeypatch.setattr(handover_watch, "FIRST_CHECK_SPEECH_SECONDS", 0.05)
    monkeypatch.setattr(handover_watch, "PAUSE_WAIT_SECONDS", 5.0)
    check, calls = _check(("leave a message", AMDCategory.MACHINE_VM))
    script = [
        VADEventType.START_OF_SPEECH,
        VADEventType.END_OF_SPEECH,
        VADEventType.START_OF_SPEECH,
    ]
    script += [VADEventType.INFERENCE_DONE] * 100 + [VADEventType.END_OF_SPEECH]
    machine, _ = await asyncio.wait_for(_verdict(check, script), timeout=3)
    assert machine is True
    # Checked at the second pause (frame ~103), not at 0.05 s of speech.
    assert calls[0] >= 100 * 320


async def test_a_blip_does_not_use_up_a_check(monkeypatch, leg) -> None:
    """Check 1 waits for FIRST_CHECK_SPEECH_SECONDS of speech; a blip and
    silence never get there."""
    monkeypatch.setattr(handover_watch, "FIRST_CHECK_SPEECH_SECONDS", 0.2)
    monkeypatch.setattr(handover_watch, "RECHECK_TIMEOUT_SECONDS", 0.1)
    check, calls = _check(("Hi", AMDCategory.HUMAN))
    vad, _ = _vad([VADEventType.START_OF_SPEECH, VADEventType.END_OF_SPEECH])
    line = handover_watch.ContactLine(
        object(), "human-x", vad, check=check, window_seconds=0.4
    )
    assert await line.speaks(1.0)
    assert await asyncio.wait_for(line.machine(), timeout=2) is False
    assert calls == []
    await line.aclose()


async def test_under_the_speech_minimum_never_checks(monkeypatch, leg) -> None:
    """Speech that stops before FIRST_CHECK_SPEECH_SECONDS, pause or not,
    runs no check: the line connects as before."""
    monkeypatch.setattr(handover_watch, "FIRST_CHECK_SPEECH_SECONDS", 0.5)
    monkeypatch.setattr(handover_watch, "RECHECK_TIMEOUT_SECONDS", 0.1)
    check, calls = _check(("not available", AMDCategory.MACHINE_UNAVAILABLE))
    script = [VADEventType.START_OF_SPEECH] + [VADEventType.INFERENCE_DONE] * 80
    script += [VADEventType.END_OF_SPEECH]
    vad, _ = _vad(script)
    line = handover_watch.ContactLine(
        object(), "human-x", vad, check=check, window_seconds=0.6
    )
    assert await line.speaks(1.0)
    assert await asyncio.wait_for(line.machine(), timeout=2) is False
    assert calls == [] and line.verdict()["reason"] == "window_ended"
    await line.aclose()


async def test_end_ends_speaks_at_once(leg) -> None:
    vad, _ = _vad([])
    line = handover_watch.ContactLine(object(), "human-x", vad)
    waiting = asyncio.ensure_future(line.speaks(30.0))
    await asyncio.sleep(0.01)
    line.end()
    assert await asyncio.wait_for(waiting, timeout=0.5) is False
    await line.aclose()


async def test_enough_speech_gets_checked(monkeypatch, leg) -> None:
    monkeypatch.setattr(handover_watch, "FIRST_CHECK_SPEECH_SECONDS", 0.05)
    check, calls = _check(("leave a message", AMDCategory.MACHINE_VM))
    script = [VADEventType.START_OF_SPEECH] + [VADEventType.INFERENCE_DONE] * 200
    machine, _ = await asyncio.wait_for(_verdict(check, script), timeout=2)
    assert machine is True and len(calls) == 1


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
    # Always Hail's one-shot model: an org model may be streaming-only.
    for model in ("nova-3-medical", "flux-general-en"):
        assert handover_watch._build_stt(
            {"stt": _layer("deepgram", api_key="dg-org", params={"model": model})}
        )
    # Another provider: its audio never goes to a provider it did not pick.
    assert handover_watch._build_stt({"stt": _layer("speechmatics")}) is None
    # Hail's key only when the org allows fallback.
    assert handover_watch._build_stt({"stt": _layer("deepgram")}) is None
    assert handover_watch._build_stt({"stt": _layer("deepgram", fallback_enabled=True)})
    assert deepgram == [
        ("nova-3", "dg-house"),
        ("nova-3", "dg-org"),
        ("nova-3", "dg-org"),
        ("nova-3", "dg-org"),
        ("nova-3", "dg-house"),
    ]


def test_no_deepgram_key_means_no_check(monkeypatch) -> None:
    monkeypatch.setattr(handover_watch.settings, "deepgram_api_key", "")
    models = handover_watch.build_check_models(SimpleNamespace(llm=None))
    assert handover_watch.greeting_check(models) is None and models.owned == []


async def test_no_classifier_means_no_check(monkeypatch, deepgram) -> None:
    monkeypatch.setattr(handover_watch, "build_classifier", lambda *a, **k: (None, []))
    models = handover_watch.build_check_models(SimpleNamespace(llm=None))
    assert handover_watch.greeting_check(models) is None
    assert len(models.owned) == 1  # the transcriber, to close
    await models.aclose()
    models.recheck_stt.aclose.assert_awaited_once()


async def test_check_uses_the_amd_prompt_for_a_partial_greeting(
    monkeypatch, deepgram
) -> None:
    llm = FakeLLM()
    backup = SimpleNamespace(aclose=AsyncMock())
    monkeypatch.setattr(
        handover_watch, "build_classifier", lambda *a, **k: (llm, [backup])
    )
    seen: dict = {}

    async def _tc(audio, *, stt, llm, prompt, note):
        seen.update(stt=stt, llm=llm, prompt=prompt, note=note)
        return "Leave a message", AMDCategory.MACHINE_VM

    monkeypatch.setattr(handover_watch, "transcribe_and_classify", _tc)
    models = handover_watch.build_check_models(SimpleNamespace(llm=None))
    check = handover_watch.greeting_check(models)
    assert check is not None
    assert await check(_frame()) == ("Leave a message", AMDCategory.MACHINE_VM)
    assert seen["llm"] is llm and seen["stt"] is models.recheck_stt
    assert seen["prompt"] == handover_watch.GREETING_CLASSIFICATION_PROMPT
    assert seen["note"] == handover_watch.PARTIAL_NOTE
    await models.aclose()
    backup.aclose.assert_awaited_once()
