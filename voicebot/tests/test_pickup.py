"""Uncertain pickup audio gets one bounded recheck before normal opening."""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from google.genai import types
from hailhq.voicebot import amd, classify, pickup
from livekit import rtc
from livekit.agents.llm import ChatChunk, ChoiceDelta, FunctionToolCall, LLMStream
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType
from livekit.agents.utils import EventEmitter
from livekit.agents.voice.amd import AMDCategory, AMDPredictionEvent
from livekit.plugins import google

from ._fakes import FakeLLM
from .test_amd import _RecordingAMD, _session_with_layers


def prediction(category="uncertain", reason="detection_timeout", speech=8.0):
    return AMDPredictionEvent(
        category=AMDCategory(category),
        reason=reason,
        speech_duration=speech,
        transcript="partial",
        delay=1.0,
    )


def frame(value=1, samples=16000):
    return rtc.AudioFrame(
        data=bytes([value, 0]) * samples,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=samples,
    )


def transcriber(text="A recorded greeting", error=None):
    return SimpleNamespace(
        recognize=AsyncMock(
            return_value=SpeechEvent(
                type=SpeechEventType.FINAL_TRANSCRIPT,
                alternatives=[SpeechData(text=text, language="")],
            ),
            side_effect=error,
        )
    )


class Classifier(FakeLLM):
    def __init__(self, args, name="save_prediction"):
        super().__init__()
        self.args, self.name = args, name
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return Stream(
            self,
            chat_ctx=kwargs["chat_ctx"],
            tools=kwargs["tools"],
            conn_options=kwargs["conn_options"],
        )


class Stream(LLMStream):
    async def _run(self):
        self._event_ch.send_nowait(
            ChatChunk(
                id="recheck",
                delta=ChoiceDelta(
                    role="assistant",
                    tool_calls=[
                        FunctionToolCall(
                            call_id="classify",
                            name=self._llm.name,
                            arguments=self._llm.args,
                        )
                    ],
                ),
            )
        )


@pytest.mark.parametrize(
    "category", ["human", "machine-vm", "machine-unavailable", "machine-ivr"]
)
async def test_whole_greeting_recheck_returns_verified_category(category):
    stt = transcriber("Laissez un message après le bip.")
    llm = Classifier(json.dumps({"label": category}))
    result = await pickup.recheck_greeting(
        prediction(),
        frame(),
        stt=stt,
        llm=llm,
        prompt="classify",
    )
    assert result.category == AMDCategory(category)
    assert result.reason == "greeting_recheck"
    assert result.transcript == "Laissez un message après le bip."
    assert result.delay >= 1.0
    call = llm.calls[0]
    assert call["chat_ctx"].items[-1].text_content == result.transcript
    assert call["tool_choice"] == "required"
    assert call["conn_options"].max_retry == 0
    assert call["conn_options"].timeout == 10.0
    assert stt.recognize.call_args.kwargs["conn_options"].max_retry == 0


async def test_google_recheck_uses_an_accepted_provider_deadline(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")
    llm = google.LLM(model="gemini-3.1-flash-lite", api_key="test-placeholder")
    deadlines = []

    async def generate(**kwargs):
        deadlines.append(kwargs["config"].http_options.timeout)

        async def chunks():
            yield types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(
                            role="model",
                            parts=[
                                types.Part(
                                    function_call=types.FunctionCall(
                                        name="save_prediction",
                                        args={"label": "machine-vm"},
                                    )
                                )
                            ],
                        ),
                        finish_reason=types.FinishReason.STOP,
                    )
                ]
            )

        return chunks()

    monkeypatch.setattr(llm._client.aio.models, "generate_content_stream", generate)
    try:
        result = await pickup.recheck_greeting(
            prediction(), frame(), stt=transcriber(), llm=llm, prompt="classify"
        )
        assert result.category == AMDCategory.MACHINE_VM
        assert deadlines == [10000]
    finally:
        await llm.aclose()


@pytest.mark.parametrize(
    "category,reason,speech",
    [
        ("human", "llm", 2),
        ("machine-vm", "llm", 9),
        ("uncertain", "no_speech_timeout", 0),
        ("uncertain", "participant_missing", 0),
        ("uncertain", "detection_timeout", 0.1),
    ],
)
async def test_positive_verdicts_and_silent_pickups_never_call_recheck(
    category, reason, speech
):
    original = prediction(category, reason, speech)
    stt = transcriber()
    result = await pickup.recheck_greeting(
        original, frame(), stt=stt, llm=FakeLLM(), prompt=""
    )
    assert result is original
    stt.recognize.assert_not_called()


async def test_missing_audio_does_not_add_provider_work():
    stt = transcriber()
    original = prediction()
    assert (
        await pickup.recheck_greeting(original, None, stt=stt, llm=FakeLLM(), prompt="")
        is original
    )
    stt.recognize.assert_not_called()


async def test_truncated_audio_does_not_add_provider_work():
    stt = transcriber()
    original = prediction()
    result = await pickup.recheck_greeting(
        original, frame(samples=320), stt=stt, llm=FakeLLM(), prompt=""
    )
    assert result is original
    stt.recognize.assert_not_called()


@pytest.mark.parametrize(
    "args,name",
    [
        ('{"label":"uncertain"}', "save_prediction"),
        ('{"label":"made-up"}', "save_prediction"),
        ('{"category":"machine-vm"}', "save_prediction"),
        ("invalid", "save_prediction"),
        ('{"label":"machine-vm"}', "end_call"),
    ],
)
async def test_invalid_or_uncertain_classifications_preserve_original(args, name):
    original = prediction()
    result = await pickup.recheck_greeting(
        original, frame(), stt=transcriber(), llm=Classifier(args, name), prompt=""
    )
    assert result is original


async def test_empty_or_failed_transcription_does_not_reach_llm(caplog):
    llm = Classifier('{"label":"human"}')
    original = prediction()
    for stt in (
        transcriber(" "),
        transcriber(error=RuntimeError("private transcript")),
    ):
        assert (
            await pickup.recheck_greeting(
                original, frame(), stt=stt, llm=llm, prompt=""
            )
            is original
        )
    assert not llm.calls
    assert "private transcript" not in caplog.text


async def test_recheck_deadline_cancels_provider_and_preserves_original(monkeypatch):
    monkeypatch.setattr(pickup, "RECHECK_TIMEOUT_SECONDS", 0.02)
    cancelled = asyncio.Event()

    async def hang(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    original = prediction()
    async with asyncio.timeout(1):
        result = await pickup.recheck_greeting(
            original,
            frame(),
            stt=SimpleNamespace(recognize=hang),
            llm=FakeLLM(),
            prompt="",
        )
    assert result is original and cancelled.is_set()


async def test_caller_cancellation_propagates_and_closes_recheck():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(
        pickup.recheck_greeting(
            prediction(),
            frame(),
            stt=SimpleNamespace(recognize=hang),
            llm=FakeLLM(),
            prompt="",
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


async def test_slow_recognize_and_slow_classify_end_at_one_total(monkeypatch):
    """One cap covers transcription and classification together."""
    monkeypatch.setattr(pickup, "RECHECK_TIMEOUT_SECONDS", 0.1)

    async def slow_recognize(*args, **kwargs):
        await asyncio.sleep(0.06)
        return SimpleNamespace(alternatives=[SimpleNamespace(text="hello")])

    class HangingStream(LLMStream):
        async def _run(self):
            await asyncio.Event().wait()

    class HangingLLM(FakeLLM):
        def chat(self, **kwargs):
            return HangingStream(
                self,
                chat_ctx=kwargs["chat_ctx"],
                tools=kwargs["tools"],
                conn_options=kwargs["conn_options"],
            )

    original = prediction()
    started = time.monotonic()
    result = await pickup.recheck_greeting(
        original,
        frame(),
        stt=SimpleNamespace(recognize=slow_recognize),
        llm=HangingLLM(),
        prompt="",
    )
    elapsed = time.monotonic() - started
    assert result is original
    # Not 0.06 + the classifier's own 5 s cap.
    assert 0.09 <= elapsed < 0.5


async def test_classifier_deadline_cancels_slow_llm(monkeypatch):
    """The check's own total also stops a slow classifier."""
    monkeypatch.setattr(classify, "CLASSIFY_TIMEOUT_SECONDS", 0.04)
    cancelled = asyncio.Event()

    class HangingStream(LLMStream):
        async def _run(self):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    class HangingLLM(FakeLLM):
        def chat(self, **kwargs):
            return HangingStream(
                self,
                chat_ctx=kwargs["chat_ctx"],
                tools=kwargs["tools"],
                conn_options=kwargs["conn_options"],
            )

    original = prediction()
    async with asyncio.timeout(1):
        result = await pickup.recheck_greeting(
            original, frame(), stt=transcriber(), llm=HangingLLM(), prompt=""
        )
    assert result is original
    assert cancelled.is_set()


async def test_capture_ignores_early_media_and_bounds_only_answered_callee_audio(
    monkeypatch,
):
    participant = SimpleNamespace(attributes={"sip.callStatus": "ringing"})
    room = SimpleNamespace(remote_participants={"callee": participant})
    wait = AsyncMock(return_value=SimpleNamespace(track="callee-track"))
    monkeypatch.setattr(pickup, "wait_for_track_publication", wait)

    class AudioStream:
        aclose = AsyncMock()

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield SimpleNamespace(frame=frame(7))  # ringback
            participant.attributes["sip.callStatus"] = "active"
            for _ in range(4):
                yield SimpleNamespace(frame=frame(3))

    stream = AudioStream()
    factory = Mock(return_value=stream)
    monkeypatch.setattr(pickup.rtc.AudioStream, "from_track", factory)
    audio = pickup.PickupAudio(room, "callee", seconds=0.04)
    audio.start()
    await audio._task
    captured = audio.frame()
    assert captured.samples_per_channel == 640
    assert captured.data.cast("B").tobytes() == bytes([3, 0]) * 640
    assert wait.call_args.kwargs["identity"] == "callee"
    assert factory.call_args.kwargs["track"] == "callee-track"
    assert "noise_cancellation" not in factory.call_args.kwargs
    stream.aclose.assert_awaited_once()
    await audio.aclose()
    assert audio.frame() is None


async def test_cancelling_capture_closes_stream_and_clears_audio(monkeypatch):
    room = SimpleNamespace(
        remote_participants={
            "callee": SimpleNamespace(attributes={"sip.callStatus": "active"})
        }
    )
    monkeypatch.setattr(
        pickup,
        "wait_for_track_publication",
        AsyncMock(return_value=SimpleNamespace(track=object())),
    )
    started = asyncio.Event()

    class AudioStream:
        aclose = AsyncMock()

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield SimpleNamespace(frame=frame())
            started.set()
            await asyncio.Event().wait()

    stream = AudioStream()
    monkeypatch.setattr(pickup.rtc.AudioStream, "from_track", Mock(return_value=stream))
    audio = pickup.PickupAudio(room, "callee", seconds=20)
    audio.start()
    await started.wait()
    await audio.aclose()
    await audio.aclose()
    stream.aclose.assert_awaited_once()
    assert audio.frame() is None


@pytest.mark.parametrize(
    "category", ["machine-unavailable", "machine-vm", "machine-ivr"]
)
async def test_run_amd_rechecks_before_owned_layers_close(monkeypatch, category):
    _RecordingAMD.result = prediction()
    monkeypatch.setattr(amd, "AMD", _RecordingAMD)
    raw = SimpleNamespace(
        start=Mock(),
        stop=AsyncMock(),
        frame=Mock(return_value=frame()),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(amd, "PickupAudio", Mock(return_value=raw))
    primary, backup = Classifier(json.dumps({"label": category})), FakeLLM()
    batch = transcriber()
    models = amd.AMDModels(
        amd.ClassifierFailover(primary, backup), object(), recheck_stt=batch
    )
    models.aclose_transcription = AsyncMock()
    call_id = UUID(int=10)
    room = object()
    session = _session_with_layers()
    emitter = EventEmitter()
    session.on, session.off = emitter.on, emitter.off
    session._start_ivr_detection = AsyncMock()
    result = await amd.run_amd(session, call_id, models=models, room=room)
    assert result.category == AMDCategory(category)
    assert result.reason == (
        "unavailable_recheck"
        if category == "machine-unavailable"
        else "greeting_recheck"
    )
    assert primary.calls
    amd.PickupAudio.assert_called_once_with(room, f"caller-{call_id}", seconds=20)
    raw.stop.assert_awaited_once()
    raw.aclose.assert_awaited_once()
    models.aclose_transcription.assert_awaited_once()
    if category == "machine-ivr":
        session._start_ivr_detection.assert_awaited_once_with(
            transcript="A recorded greeting"
        )
    else:
        session._start_ivr_detection.assert_not_called()


async def test_greeting_transcripts_during_recheck_keep_machine_verdict(
    monkeypatch,
):
    original = prediction()
    _RecordingAMD.result = original
    monkeypatch.setattr(amd, "AMD", _RecordingAMD)
    raw = SimpleNamespace(
        start=Mock(),
        stop=AsyncMock(),
        frame=Mock(return_value=frame()),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(amd, "PickupAudio", Mock(return_value=raw))
    session = _session_with_layers()
    emitter = EventEmitter()
    session.on, session.off = emitter.on, emitter.off

    async def recheck(*args, **kwargs):
        emitter.emit(
            "user_input_transcribed",
            SimpleNamespace(is_final=True, transcript="Hello? Who is calling?"),
        )
        return prediction("machine-vm", "greeting_recheck")

    monkeypatch.setattr(amd, "recheck_greeting", recheck)
    models = amd.AMDModels(FakeLLM(), object(), recheck_stt=transcriber())
    models.aclose_transcription = AsyncMock()
    result = await amd.run_amd(session, UUID(int=10), models=models, room=object())
    assert result is not original
    assert result.category == AMDCategory.MACHINE_VM
    raw.aclose.assert_awaited_once()
    models.aclose_transcription.assert_awaited_once()


async def test_unavailable_from_audio_recheck_is_verified_before_hangup(monkeypatch):
    _RecordingAMD.result = prediction()
    monkeypatch.setattr(amd, "AMD", _RecordingAMD)
    raw = SimpleNamespace(
        start=Mock(),
        stop=AsyncMock(),
        frame=Mock(return_value=frame()),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(amd, "PickupAudio", Mock(return_value=raw))
    wrong = prediction("machine-unavailable", "greeting_recheck")
    wrong.transcript = "Welcome to Delta Vacations. We're so glad you called."
    monkeypatch.setattr(amd, "recheck_greeting", AsyncMock(return_value=wrong))
    verifier = Classifier('{"label":"machine-ivr"}')
    models = amd.AMDModels(verifier, object(), recheck_stt=transcriber())
    models.aclose_transcription = AsyncMock()
    session = _session_with_layers()
    session._start_ivr_detection = AsyncMock()
    result = await amd.run_amd(session, UUID(int=10), models=models, room=object())
    assert result.category == AMDCategory.MACHINE_IVR
    assert verifier.calls
    session._start_ivr_detection.assert_awaited_once()
