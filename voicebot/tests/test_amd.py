"""Answering machine detection: construction, verdict handling, observability.

The per-category tests drive the real ``entrypoint`` with ``run_amd``
monkeypatched, so they assert the wiring (hang up vs. speak, what lands in
``captured``, what reaches ``call_events``) rather than LiveKit's classifier.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import Call, CallEvent
from hailhq.voicebot import amd as amd_mod
from hailhq.voicebot import ivr as ivr_mod
from hailhq.voicebot.amd import (
    MACHINE_HANGUP_CATEGORIES,
    NO_SPEECH_THRESHOLD_SECONDS,
    amd_end_reason,
    run_amd,
)
from hailhq.voicebot.classify import classify_speech
from livekit import rtc
from livekit.agents.llm import (
    ChatChunk,
    ChatContext,
    ChoiceDelta,
    FunctionToolCall,
    LLMStream,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.voice.amd import AMDCategory, AMDPredictionEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import Self

from ._fakes import FakeLLM
from .test_agent import _make_call_row

# --------------------------------------------------------------------------- #
# run_amd construction
# --------------------------------------------------------------------------- #


class _RecordingAMD:
    """Stand-in for livekit.agents.voice.amd.AMD recording its kwargs."""

    last_kwargs: ClassVar[dict[str, Any]] = {}
    result: AMDPredictionEvent | None = None

    def __init__(self, session: Any, **kwargs: Any) -> None:
        type(self).last_kwargs = {"session": session, **kwargs}

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self) -> AMDPredictionEvent:
        assert type(self).result is not None
        return type(self).result


def _prediction(category: str) -> AMDPredictionEvent:
    return AMDPredictionEvent(
        speech_duration=1.0,
        category=AMDCategory(category),
        reason="test",
        transcript="hello you have reached",
        delay=0.1,
    )


def _session_with_layers() -> SimpleNamespace:
    return SimpleNamespace(llm=FakeLLM(), stt=object())


async def test_run_amd_passes_session_layers_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMD must classify on the session's own brain, not LiveKit Inference."""
    _RecordingAMD.result = _prediction("human")
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)

    session = _session_with_layers()
    call_id = UUID("11111111-2222-3333-4444-5555555555aa")

    result = await run_amd(session, call_id)  # type: ignore[arg-type]

    assert result is not None and result.category == AMDCategory.HUMAN
    kwargs = _RecordingAMD.last_kwargs
    assert kwargs["session"] is session
    assert kwargs["llm"] is session.llm
    assert kwargs["stt"] is session.stt
    assert kwargs["participant_identity"] == f"caller-{call_id}"
    assert kwargs["detection_options"] == {
        "timeout": amd_mod.POST_ANSWER_TIMEOUT_SECONDS,
        "no_speech_threshold": NO_SPEECH_THRESHOLD_SECONDS,
        "human_speech_threshold": 0.0,
        "prompt": amd_mod.GREETING_CLASSIFICATION_PROMPT,
    }
    assert kwargs["wait_until_finished"] is False
    assert not kwargs.get("suppress_compatibility_warning", False)
    # ivr_detection is left at LiveKit's default (True) on purpose.
    assert "ivr_detection" not in kwargs


async def test_run_amd_returns_none_when_detection_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detection failure must never kill the call."""

    class _Boom:
        def __init__(self, *_a: object, **_k: object) -> None:
            raise RuntimeError("classifier exploded")

    monkeypatch.setattr(amd_mod, "AMD", _Boom)

    assert await run_amd(_session_with_layers(), UUID(int=1)) is None  # type: ignore[arg-type]


async def test_audio_ready_is_published_after_amd_is_armed(monkeypatch):
    order = []

    class Detector(_RecordingAMD):
        async def __aenter__(self):
            order.append("armed")
            return self

        async def execute(self):
            order.append("classify")
            return _prediction("human")

    async def ready():
        order.append("ready")

    monkeypatch.setattr(amd_mod, "AMD", Detector)
    await run_amd(_session_with_layers(), UUID(int=1), on_ready=ready)
    assert order == ["armed", "ready", "classify"]


async def test_post_answer_deadline_releases_continuous_speech(monkeypatch):
    """The real classifier must release speech even without end-of-turn."""
    from livekit.agents.voice.amd.classifier import _AMDClassifier

    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    monkeypatch.setattr(amd_mod, "POST_ANSWER_TIMEOUT_SECONDS", 0.03)
    _RecordingAMD.result = _prediction("human")
    await run_amd(_session_with_layers(), UUID(int=1))
    kwargs = _RecordingAMD.last_kwargs
    classifier = _AMDClassifier(
        llm=FakeLLM(),
        wait_until_finished=kwargs["wait_until_finished"],
        **kwargs["detection_options"],
    )
    try:
        # Constructing/arming AMD before the dial must not consume its budget.
        await asyncio.sleep(0.04)
        assert not classifier._verdict_ready.is_set()
        classifier.start_detection_timer()
        classifier.start_listening()
        classifier.on_user_speech_started()
        await asyncio.wait_for(classifier._verdict_ready.wait(), timeout=0.2)
        assert classifier._verdict_result.category == AMDCategory.UNCERTAIN
        assert classifier._verdict_result.reason == "detection_timeout"
    finally:
        await classifier.close()


class _GreetingLLM(FakeLLM):
    def __init__(self, category: str):
        super().__init__()
        self.category = category
        self.transcripts: list[str] = []

    def chat(self, **kwargs):
        self.transcripts.append(kwargs["chat_ctx"].items[-1].text_content)
        return _GreetingStream(
            self,
            chat_ctx=kwargs["chat_ctx"],
            tools=kwargs["tools"],
            conn_options=DEFAULT_API_CONNECT_OPTIONS,
        )


class _GreetingStream(LLMStream):
    async def _run(self):
        self._event_ch.send_nowait(
            ChatChunk(
                id="greeting",
                delta=ChoiceDelta(
                    role="assistant",
                    tool_calls=[
                        FunctionToolCall(
                            call_id="greeting",
                            name="save_prediction",
                            arguments=json.dumps({"label": self._llm.category}),
                        )
                    ],
                ),
            )
        )


async def _timed_classifier(monkeypatch, llm):
    """Use production detection options with time scaled 10x for SDK tests."""
    from livekit.agents.voice.amd.classifier import _AMDClassifier

    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    _RecordingAMD.result = _prediction("human")
    await run_amd(_session_with_layers(), UUID(int=1))
    kwargs = _RecordingAMD.last_kwargs
    options = dict(kwargs["detection_options"])
    options["timeout"] *= 0.1
    options["no_speech_threshold"] *= 0.1
    classifier = _AMDClassifier(
        llm=llm,
        wait_until_finished=kwargs["wait_until_finished"],
        max_endpointing_delay=0.05,
        machine_silence_threshold=0.04,
        **options,
    )
    classifier.start_detection_timer()
    classifier.start_listening()
    return classifier


@pytest.mark.parametrize(
    "category,transcript",
    [
        (
            "machine-vm",
            (
                "Cannot take your call right now. Leave a message after the tone. "
                "When finished, press the pound key or hang up."
            ),
        ),
        ("machine-ivr", "Welcome to our office. For reception, press one."),
    ],
)
async def test_long_greeting_waits_for_transcript_and_machine_verdict(
    monkeypatch, category, transcript
):
    """A nine-second greeting must reach the classifier before fallback wins."""
    llm = _GreetingLLM(category)
    classifier = await _timed_classifier(monkeypatch, llm)
    try:
        classifier.on_user_speech_started()
        await asyncio.sleep(0.9)
        assert not classifier._verdict_ready.is_set()
        classifier.on_user_speech_ended(silence_duration=0)
        classifier.push_text(transcript)
        classifier.on_end_of_turn()
        await asyncio.wait_for(classifier._verdict_ready.wait(), timeout=0.5)
        assert classifier._verdict_result.category == AMDCategory(category)
        assert classifier._verdict_result.reason == "llm"
        assert classifier._verdict_result.transcript == transcript
        assert llm.transcripts == [transcript]
    finally:
        await classifier.close()


async def test_short_human_greeting_does_not_wait_for_detection_deadline(monkeypatch):
    classifier = await _timed_classifier(monkeypatch, _GreetingLLM("human"))
    try:
        classifier.on_user_speech_started()
        await asyncio.sleep(0.01)
        classifier.on_user_speech_ended(silence_duration=0)
        classifier.push_text("Hello?")
        await asyncio.wait_for(classifier._verdict_ready.wait(), timeout=0.3)
        assert classifier._verdict_result.category == AMDCategory.HUMAN
        assert classifier._verdict_result.reason == "llm"
    finally:
        await classifier.close()


async def test_silent_pickup_keeps_three_second_limit(monkeypatch):
    llm = _GreetingLLM("human")
    classifier = await _timed_classifier(monkeypatch, llm)
    try:
        await asyncio.wait_for(classifier._verdict_ready.wait(), timeout=0.6)
        assert classifier._verdict_result.category == AMDCategory.UNCERTAIN
        assert classifier._verdict_result.reason == "no_speech_timeout"
        assert llm.transcripts == []
    finally:
        await classifier.close()


async def test_continuous_speech_remains_bounded_with_production_options(monkeypatch):
    llm = _GreetingLLM("human")
    classifier = await _timed_classifier(monkeypatch, llm)
    try:
        classifier.on_user_speech_started()
        await asyncio.wait_for(classifier._verdict_ready.wait(), timeout=2.5)
        assert classifier._verdict_result.category == AMDCategory.UNCERTAIN
        assert classifier._verdict_result.reason == "detection_timeout"
        assert llm.transcripts == []
    finally:
        await classifier.close()


def test_prompt_calls_a_short_live_greeting_a_person() -> None:
    flat = " ".join(amd_mod.GREETING_CLASSIFICATION_PROMPT.split())
    for greeting in ("'Hi'", "'Hello?'", "'Yes?'", "'Hej?'", "'Allô ?'"):
        assert greeting in flat
    assert "A short live greeting that ends and waits for a reply" in flat
    assert "do not guess human just because the greeting is short" not in flat
    # A greeting that continues still waits for the voicemail cues.
    assert "wait for the voicemail cues" in flat
    for kept in (
        "an invitation to leave a message",
        "a tone",
        "you've reached",
        "not available",
        "machine-vm:",
        "machine-ivr:",
        "machine-unavailable:",
        "laissez un message après le bip",
        "clearly mid-sentence, use postpone_termination",
    ):
        assert kept in flat


async def test_dial_gate_is_released_when_amd_is_skipped() -> None:
    """A session without a plain LLM skips AMD but must still let the API dial."""
    released = []

    async def ready():
        released.append(True)

    session = SimpleNamespace(llm=object(), stt=object())
    assert await run_amd(session, UUID(int=1), on_ready=ready) is None  # type: ignore[arg-type]
    assert released == [True]


async def test_dial_gate_is_released_once_when_amd_fails_to_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released = []

    class Broken(_RecordingAMD):
        async def __aenter__(self):
            raise ValueError("classifier could not be resolved")

    async def ready():
        released.append(True)

    monkeypatch.setattr(amd_mod, "AMD", Broken)
    assert await run_amd(_session_with_layers(), UUID(int=1), on_ready=ready) is None  # type: ignore[arg-type]
    assert released == [True]


async def test_run_amd_gives_up_when_detection_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMD's own budget arms only once the SIP track publishes; a leg that
    never publishes would otherwise block entrypoint forever."""

    class _NeverSettles(_RecordingAMD):
        async def execute(self) -> AMDPredictionEvent:
            await asyncio.Event().wait()  # pragma: no cover — never returns
            raise AssertionError("unreachable")

    monkeypatch.setattr(amd_mod, "AMD", _NeverSettles)
    monkeypatch.setattr(amd_mod, "DETECTION_TIMEOUT_SECONDS", 0.01)

    assert await run_amd(_session_with_layers(), UUID(int=4)) is None  # type: ignore[arg-type]


async def test_run_amd_skips_when_session_has_no_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = SimpleNamespace(llm=None, stt=object())
    assert await run_amd(session, UUID(int=2)) is None  # type: ignore[arg-type]


async def test_run_amd_skips_when_session_has_no_stt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = SimpleNamespace(llm=FakeLLM(), stt=None)
    assert await run_amd(session, UUID(int=3)) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# category → outcome mapping
# --------------------------------------------------------------------------- #


def test_only_the_two_unreachable_machine_categories_hang_up() -> None:
    assert MACHINE_HANGUP_CATEGORIES == {"machine-vm", "machine-unavailable"}
    # machine-ivr proceeds: AMD has already started IVR navigation and the
    # tree may still route to a person.
    assert AMDCategory.MACHINE_IVR not in MACHINE_HANGUP_CATEGORIES
    assert AMDCategory.UNCERTAIN not in MACHINE_HANGUP_CATEGORIES
    assert AMDCategory.HUMAN not in MACHINE_HANGUP_CATEGORIES


def test_amd_end_reason_maps_to_call_end_reason_values() -> None:
    assert amd_end_reason("machine-vm") == CallEndReason.VOICEMAIL_REACHED.value
    assert (
        amd_end_reason("machine-unavailable") == CallEndReason.MACHINE_UNAVAILABLE.value
    )


async def test_unavailable_business_welcome_is_rechecked_before_hangup(monkeypatch):
    original = _prediction("machine-unavailable")
    original.transcript = "Welcome to Delta Vacations. We're so glad you called."
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = _session_with_layers()
    session.llm = _GreetingLLM("machine-ivr")
    session._start_ivr_detection = AsyncMock()
    result = await run_amd(session, UUID(int=1))
    assert result.category == AMDCategory.MACHINE_IVR
    assert result.reason == "unavailable_recheck"
    session._start_ivr_detection.assert_awaited_once()


async def test_explicit_unavailable_verdict_is_preserved_after_recheck(monkeypatch):
    original = _prediction("machine-unavailable")
    original.transcript = "This mailbox is full and cannot accept messages."
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = _session_with_layers()
    session.llm = _GreetingLLM("machine-unavailable")
    result = await run_amd(session, UUID(int=1))
    assert result.category == AMDCategory.MACHINE_UNAVAILABLE


def _break_checker(monkeypatch, session, failure: str) -> None:
    """Make the unavailable recheck fail without giving a verdict."""
    if failure == "provider-error":
        session.llm.chat = Mock(side_effect=RuntimeError("classifier failed"))
    elif failure == "timeout":

        # The check hit its five-second total.
        monkeypatch.setattr(
            ivr_mod, "classify_speech", AsyncMock(side_effect=TimeoutError)
        )


async def test_answered_uncertain_unavailable_recheck_hangs_up(monkeypatch):
    original = _prediction("machine-unavailable")
    original.transcript = "The number you have dialed is not in service."
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = _session_with_layers()
    session.llm = _GreetingLLM("uncertain")
    session._start_ivr_detection = AsyncMock()
    result = await run_amd(session, UUID(int=1))
    assert result.category == AMDCategory.MACHINE_UNAVAILABLE
    session._start_ivr_detection.assert_not_awaited()


@pytest.mark.parametrize("failure", ["provider-error", "timeout"])
async def test_failed_unavailable_recheck_keeps_listening_as_menu(monkeypatch, failure):
    original = _prediction("machine-unavailable")
    original.transcript = "The number you have dialed is not in service."
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)
    session = _session_with_layers()
    session.llm = _GreetingLLM("machine-unavailable")
    _break_checker(monkeypatch, session, failure)
    session._start_ivr_detection = AsyncMock()
    result = await run_amd(session, UUID(int=1))
    assert result.category == AMDCategory.MACHINE_IVR
    assert result.reason == "unavailable_recheck"
    session._start_ivr_detection.assert_awaited_once()


async def test_answered_uncertain_unavailable_recheck_ends_call_without_greeting(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checker answered unsure: keep the unavailable verdict, hang up now."""
    original = _prediction("machine-unavailable")
    original.transcript = "The number you have dialed is not in service."
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)

    async def detect(session, call_id, **kwargs):
        session.stt = object()
        session.llm = _GreetingLLM("uncertain")
        session._start_ivr_detection = AsyncMock()
        return await run_amd(session, call_id)

    call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=None, run_amd_override=detect
    )
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == [CallEndReason.MACHINE_UNAVAILABLE.value]
    assert session.say_calls == []
    assert session.replies == []
    events = await _amd_events(async_session, call_id)
    assert events[0].payload["category"] == "machine-unavailable"


@pytest.mark.parametrize("verification", ["machine-ivr", "provider-error", "timeout"])
async def test_atlantis_welcome_misclassified_unavailable_keeps_late_menu_alive(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, verification: str
) -> None:
    """Call 435dd50f: the menu arrived one second after the unavailable verdict.

    Exercise the real AMD verification and entrypoint outcome together. A
    machine-ivr recheck, or a recheck that fails without a verdict, must keep
    the call in menu mode. A missing recheck must terminate this call and fail
    the test.
    """
    welcome = (
        "Thank you for calling Atlantis Paradise Island, Bahamas. Your call may "
        "be recorded for quality assurance and training purposes."
    )
    menu = (
        "Please listen carefully as our menu options have recently changed. "
        "For existing reservations made directly with Atlantis, please press one. "
        "To make a new reservation, please press two."
    )
    original = _prediction("machine-unavailable")
    original.transcript = welcome
    _RecordingAMD.result = original
    monkeypatch.setattr(amd_mod, "AMD", _RecordingAMD)

    async def detect(session, call_id, **kwargs):
        session.stt = object()
        session.llm = _GreetingLLM("machine-unavailable")
        if verification == "machine-ivr":
            session.llm = _GreetingLLM(verification)
        else:
            _break_checker(monkeypatch, session, verification)
        session._start_ivr_detection = AsyncMock()
        return await run_amd(session, call_id)

    call_id, ctx, session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=None,
        run_amd_override=detect,
        shutdown=False,
    )
    # The later menu turns use a working checker.
    monkeypatch.setattr(ivr_mod, "classify_speech", classify_speech)
    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []
    assert session.say_calls == []
    assert welcome in session.replies[0]
    events = await _amd_events(async_session, call_id)
    assert events[0].payload["category"] == "machine-ivr"
    assert events[0].payload["reason"] == "unavailable_recheck"

    # The rest of the recording arrives after AMD has returned. It must stay
    # in menu mode, without speaking a disclosure or starting a hangup.
    agent = session.start_options["agent"]
    agent.phone_menu.llm = _GreetingLLM("machine-ivr")
    session.emit_vad_speaking()
    session.emit_user_turn(menu)
    await session.drain_turns()
    assert agent.phone_menu.active
    assert agent.phone_menu.can_navigate
    assert session.say_calls == []
    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []
    await agent.phone_menu.aclose()


# --------------------------------------------------------------------------- #
# entrypoint wiring
# --------------------------------------------------------------------------- #


class _FakeAmdSession:
    """AgentSession stand-in: event registration, start(), say(), reply."""

    def __init__(self) -> None:
        self.say_calls: list[str] = []
        self.replies: list[str] = []
        self.started = False
        self.handlers: dict[str, list] = {}
        self.llm = _GreetingLLM("human")
        self.turn_tasks: set[asyncio.Task] = set()

    def on(self, event: str):
        def _register(fn):
            self.handlers.setdefault(event, []).append(fn)
            return fn

        return _register

    def emit_user_turn(self, text: str = "Hello.") -> None:
        item = SimpleNamespace(role="user", text_content=text)
        for fn in self.handlers.get("conversation_item_added", []):
            fn(SimpleNamespace(item=item))
        agent = self.start_options.get("agent")
        if agent is not None and agent.phone_menu is not None:
            task = asyncio.create_task(
                agent.on_user_turn_completed(ChatContext(), item)
            )
            self.turn_tasks.add(task)
            task.add_done_callback(self.turn_tasks.discard)

    async def drain_turns(self):
        await asyncio.gather(*list(self.turn_tasks))

    def emit_keypress(self, name: str = "send_dtmf") -> None:
        ev = SimpleNamespace(function_calls=[SimpleNamespace(name=name)])
        for fn in self.handlers.get("function_tools_executed", []):
            fn(ev)

    def emit_vad_speaking(self) -> None:
        """VAD heard a voice — no transcript, as with unsupported languages."""
        ev = SimpleNamespace(new_state="speaking", old_state="listening")
        for fn in self.handlers.get("user_state_changed", []):
            fn(ev)

    def generate_reply(self, *, instructions: str) -> Any:
        self.replies.append(instructions)
        return SimpleNamespace()

    async def start(self, **_kwargs: object) -> None:
        self.started = True
        self.start_options = _kwargs

    def clear_user_turn(self) -> None:
        pass

    def say(self, text: str, *, allow_interruptions: bool = True) -> Any:
        self.say_calls.append(text)

        async def _resolve() -> None:
            return None

        return _resolve()


class _FakeLocalParticipant:
    def __init__(self) -> None:
        self.dtmf: list[tuple[int, str]] = []

    async def publish_dtmf(self, *, code: int, digit: str) -> None:
        self.dtmf.append((code, digit))


class _FakeAmdRoom:
    def __init__(self, name: str, *, disconnect_on_register: bool = False) -> None:
        self.name = name
        self.remote_participants: dict[str, object] = {}
        self.local_participant = _FakeLocalParticipant()
        self._disconnect_on_register = disconnect_on_register

    def on(self, event: str):
        def _register(fn):
            # Mirrors production ordering on a rejected leg: the SIP
            # participant is already gone by the time entrypoint wires its
            # handlers, so the disconnect fires before AMD would start.
            if event == "participant_disconnected" and self._disconnect_on_register:
                fn(
                    SimpleNamespace(
                        identity="sip-callee",
                        kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
                        disconnect_reason=rtc.DisconnectReason.USER_REJECTED,
                    )
                )
            return fn

        return _register


class _FakeAmdCtx:
    def __init__(
        self, metadata: str, room_name: str, *, disconnect_on_register: bool = False
    ) -> None:
        self.job = SimpleNamespace(metadata=metadata)
        self.room = _FakeAmdRoom(
            room_name, disconnect_on_register=disconnect_on_register
        )
        self.proc = SimpleNamespace(userdata={"vad": object()})
        self.shutdown_calls: list[str] = []
        self.delete_room_calls = 0
        self.shutdown_callbacks: list[Any] = []
        self.api = SimpleNamespace()

    async def connect(self) -> None:
        return None

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)

    async def delete_room(self) -> None:
        self.delete_room_calls += 1

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)


async def _drive_entrypoint(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    amd_result: AMDPredictionEvent | None,
    run_amd_override: Any = None,
    disconnect_before_amd: bool = False,
    answered: bool = False,
    classifier: Any = "fake",
    real_amd_models: bool = False,
    shutdown: bool = True,
) -> tuple[UUID, _FakeAmdCtx, _FakeAmdSession]:
    """Run ``entrypoint`` with AMD stubbed, then fire the shutdown callback.

    ``shutdown=False`` keeps the call up, for tests that drive later turns:
    shutdown closes the phone menu."""
    from hailhq.voicebot import agent as agent_mod

    org_id = UUID("11111111-2222-3333-4444-555555555555")
    call_id = await _make_call_row(async_session)
    session = _FakeAmdSession()
    # AMD's classifier also backs the menu check; it hears a person.
    models = amd_mod.AMDModels(
        _GreetingLLM("human") if classifier == "fake" else classifier, object()
    )
    models.aclose = AsyncMock()
    session.amd_models = models

    async def _no_org_cfgs(_org_id: UUID | None, *, skip_llm: bool = False) -> dict:
        return {}

    async def _fake_run_amd(_session: object, _call_id: UUID, **kwargs):
        return amd_result

    monkeypatch.setattr(agent_mod, "resolve_org_configs", _no_org_cfgs)
    monkeypatch.setattr(agent_mod, "build_session", lambda *a, **k: session)
    if not real_amd_models:
        monkeypatch.setattr(agent_mod, "build_amd_models", lambda *a, **k: models)
    monkeypatch.setattr(agent_mod, "run_amd", run_amd_override or _fake_run_amd)
    monkeypatch.setattr(
        agent_mod.settings, "hail_voice_max_duration_seconds", 0, raising=False
    )

    ctx = _FakeAmdCtx(
        metadata=json.dumps(
            {
                "call_id": str(call_id),
                "organization_id": str(org_id),
                "tools": [],
                "first_message": "Is this a good time?",
            }
        ),
        room_name=f"hail-{call_id}",
        disconnect_on_register=disconnect_before_amd,
    )
    if answered:
        ctx.room.remote_participants["caller"] = SimpleNamespace(
            identity=f"caller-{call_id}",
            kind=rtc.ParticipantKind.PARTICIPANT_KIND_SIP,
            attributes={"sip.callStatus": "active"},
        )

    await entrypoint_under_test(ctx)

    # The job releases after entrypoint returns; run the callback the same
    # way the SDK would so on_call_end lands.
    for cb in ctx.shutdown_callbacks if shutdown else []:
        await cb()
    await asyncio.sleep(0)
    return call_id, ctx, session


def entrypoint_under_test(ctx: Any):
    from hailhq.voicebot.agent import entrypoint

    return entrypoint(ctx)


@pytest.mark.parametrize("answered", [False, True])
async def test_noise_recovery_preserves_recording_lifecycle(
    async_session, monkeypatch, answered
):
    from hailhq.voicebot import agent as agent_mod

    monkeypatch.setattr(agent_mod.settings, "livekit_url", "wss://test.livekit.cloud")
    monkeypatch.setattr(agent_mod, "mark_call_answered", AsyncMock(return_value=True))
    start = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(agent_mod, "start_recording", start)
    monkeypatch.setattr(agent_mod, "stop_recording", stop)
    recovery = Mock(aclose=AsyncMock())
    monkeypatch.setattr(agent_mod, "ConversationRecovery", Mock(return_value=recovery))

    call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("human"), answered=answered
    )

    assert session.start_options["record"] is False
    assert session.start_options["session_host"] is False
    assert session.start_options["room_options"].audio_input.noise_cancellation
    recovery.start.assert_called_once()
    recovery.aclose.assert_awaited_once()
    if answered:
        start.assert_awaited_once_with(call_id, ctx.room.name, ctx.api)
        stop.assert_awaited_once_with(call_id, ctx.api, ctx.room.name)
    else:
        start.assert_not_awaited()
        stop.assert_not_awaited()


async def _amd_events(async_session: AsyncSession, call_id: UUID) -> list[CallEvent]:
    return list(
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "amd_result"
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize(
    "category,end_reason",
    [
        ("machine-vm", CallEndReason.VOICEMAIL_REACHED.value),
        ("machine-unavailable", CallEndReason.MACHINE_UNAVAILABLE.value),
    ],
)
async def test_machine_categories_hang_up_without_speaking(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    end_reason: str,
) -> None:
    call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction(category)
    )

    # Nothing was said — no disclosure, no first_message, no voicemail left.
    assert session.say_calls == []
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == [end_reason]

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "no_answer"
    assert refreshed.end_reason == end_reason


@pytest.mark.parametrize("category", ["human", "uncertain"])
async def test_non_hangup_categories_speak_the_disclosure(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
) -> None:
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction(category)
    )

    assert session.say_calls == [f"{AI_DISCLOSURE_LINE} Is this a good time?"]
    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []


async def test_failed_greeting_is_not_saved_as_no_answer(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A say()/TTS failure after a person answered is not an unanswered call."""

    def broken_say(self: Any, text: str, **kwargs: Any) -> Any:
        raise RuntimeError("tts failed")

    monkeypatch.setattr(_FakeAmdSession, "say", broken_say)
    call_id, ctx, _session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("human")
    )

    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []
    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status != "no_answer"


# --------------------------------------------------------------------------- #
# machine-ivr: the production failure this branch fixes
# --------------------------------------------------------------------------- #


async def test_amd_is_skipped_when_the_sip_leg_already_disconnected(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: call 3c7e39b7 was rejected at :20.475, yet AMD still ran
    to its 30s backstop and the job had to be force-cancelled."""
    ran = {"amd": False}

    async def _tracking_run_amd(_session: object, _call_id: UUID, **kwargs):
        ran["amd"] = True

    call_id, _ctx, _session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=None,
        run_amd_override=_tracking_run_amd,
        disconnect_before_amd=True,
    )

    assert ran["amd"] is False
    events = await _amd_events(async_session, call_id)
    assert len(events) == 1
    assert events[0].payload == {
        "category": None,
        "transcript": None,
        "reason": "detection_failed",
        "delay": None,
    }


async def test_ivr_does_not_speak_the_greeting_into_the_menu(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: call fe1d2d08 recited the disclosure at a "press one /
    press two" menu and pressed nothing, because `session.say` is TTS-only
    and never gives the LLM a turn."""
    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("machine-ivr")
    )

    # Nothing spoken at the tree...
    assert session.say_calls == []
    # ...and the LLM got a real turn, which is the only thing that can
    # reach the send_dtmf tool.
    assert len(session.replies) == 1
    assert ctx.shutdown_calls == []


async def test_ivr_reply_carries_the_menu_and_orders_keypresses(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _call_id, _ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("machine-ivr")
    )

    instructions = session.replies[0]
    assert "hello you have reached" in instructions  # the captured menu
    assert "send_dtmf" in instructions
    assert "do not speak" in instructions.lower()


async def test_vad_without_transcript_cannot_confirm_a_person(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VAD alone does not greet at once; PhoneMenu waits a bounded time (test_ivr.py)."""

    _call_id, _ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("machine-ivr")
    )

    session.emit_keypress()
    session.emit_vad_speaking()  # a voice, zero transcripts
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session.say_calls == []


async def test_greeting_stays_held_until_keys_are_pressed(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice activity from the tail of the menu prompt must not trigger it."""
    _call_id, _ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction("machine-ivr")
    )

    session.emit_vad_speaking()  # menu still talking, no keypress yet
    session.emit_user_turn("For any other request, press two.")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert session.say_calls == []


async def test_greeting_fires_once_across_both_triggers(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, _ctx, session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=_prediction("machine-ivr"),
        shutdown=False,
    )

    session.emit_keypress()
    for _ in range(3):
        session.emit_vad_speaking()
        session.emit_user_turn("Hola?")
        await asyncio.sleep(0)
    await session.drain_turns()

    assert len(session.say_calls) == 1
    assert session.say_calls[0].startswith(AI_DISCLOSURE_LINE)


async def test_ivr_greeting_is_deferred_to_the_first_person(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disclosure is delayed, never dropped."""
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, _ctx, session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=_prediction("machine-ivr"),
        shutdown=False,
    )
    assert session.say_calls == []

    session.emit_keypress()
    session.emit_user_turn("Hello, reception.")
    await session.drain_turns()

    assert session.say_calls == [f"{AI_DISCLOSURE_LINE} Is this a good time?"]


async def test_detection_failure_proceeds_like_a_human_answer(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=None
    )

    assert session.say_calls[0].startswith(AI_DISCLOSURE_LINE)
    assert ctx.delete_room_calls == 0


def _count_amd_models(monkeypatch, models) -> list:
    """Hand ``models`` to the entrypoint and count the builds."""
    from hailhq.voicebot import agent as agent_mod

    built: list = []
    monkeypatch.setattr(
        agent_mod, "build_amd_models", lambda *a, **k: built.append(1) or models
    )
    return built


def _record_after_entrypoint(monkeypatch, seen: dict, models) -> None:
    """Note whether the classifier was closed before shutdown callbacks run."""
    import sys

    from hailhq.voicebot.agent import entrypoint

    async def run_then_check(ctx):
        await entrypoint(ctx)
        seen["closed_before_shutdown"] = models.aclose.await_count

    monkeypatch.setattr(sys.modules[__name__], "entrypoint_under_test", run_then_check)


async def test_menu_check_reuses_amd_classifier_until_call_end(
    async_session, monkeypatch
):
    """One classifier per call: AMD's chain backs the menu check and closes
    at call end, after the menu."""
    models = amd_mod.AMDModels(_GreetingLLM("machine-ivr"), object())
    models.aclose = AsyncMock()
    built = _count_amd_models(monkeypatch, models)
    seen: dict = {}
    _record_after_entrypoint(monkeypatch, seen, models)
    _, _ctx, session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=_prediction("machine-ivr"),
        real_amd_models=True,
    )
    assert built == [1]
    assert session.start_options["agent"].phone_menu.llm is models.llm
    assert seen == {"closed_before_shutdown": 0}
    models.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "category", ["human", "uncertain", "machine-vm", "machine-unavailable", None]
)
async def test_classifier_closes_once_at_call_end_on_every_path(
    async_session, monkeypatch, category
):
    """The classifier closes in one place: shutdown, once per call."""
    models = amd_mod.AMDModels(_GreetingLLM("human"), object())
    models.aclose = AsyncMock()
    _count_amd_models(monkeypatch, models)
    seen: dict = {}
    _record_after_entrypoint(monkeypatch, seen, models)
    await _drive_entrypoint(
        async_session,
        monkeypatch,
        amd_result=_prediction(category) if category else None,
        real_amd_models=True,
    )
    assert seen == {"closed_before_shutdown": 0}
    models.aclose.assert_awaited_once()


@pytest.mark.parametrize(
    "category",
    ["human", "uncertain", "machine-ivr", "machine-vm", "machine-unavailable"],
)
async def test_amd_result_event_written_on_every_path(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, category: str
) -> None:
    call_id, _ctx, _session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction(category)
    )

    events = await _amd_events(async_session, call_id)
    assert len(events) == 1
    assert events[0].payload == {
        "category": category,
        "transcript": "hello you have reached",
        "reason": "test",
        "delay": 0.1,
    }


async def test_amd_result_event_written_when_detection_failed(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_id, _ctx, _session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=None
    )

    events = await _amd_events(async_session, call_id)
    assert len(events) == 1
    assert events[0].payload == {
        "category": None,
        "transcript": None,
        "reason": "detection_failed",
        "delay": None,
    }
