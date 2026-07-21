"""Answering machine detection: construction, verdict handling, observability.

The per-category tests drive the real ``entrypoint`` with ``run_amd``
monkeypatched, so they assert the wiring (hang up vs. speak, what lands in
``captured``, what reaches ``call_events``) rather than LiveKit's classifier.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from livekit.agents.voice.amd import AMDCategory, AMDPredictionEvent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import Call, CallEvent
from hailhq.voicebot import amd as amd_mod
from hailhq.voicebot.amd import (
    MACHINE_HANGUP_CATEGORIES,
    NO_SPEECH_THRESHOLD_SECONDS,
    amd_end_reason,
    run_amd,
)

from ._fakes import FakeLLM
from .test_agent import _make_call_row

# --------------------------------------------------------------------------- #
# run_amd construction
# --------------------------------------------------------------------------- #


class _RecordingAMD:
    """Stand-in for livekit.agents.voice.amd.AMD recording its kwargs."""

    last_kwargs: dict[str, Any] = {}
    result: AMDPredictionEvent | None = None

    def __init__(self, session: Any, **kwargs: Any) -> None:
        type(self).last_kwargs = {"session": session, **kwargs}

    async def __aenter__(self) -> "_RecordingAMD":
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
        "no_speech_threshold": NO_SPEECH_THRESHOLD_SECONDS
    }
    assert kwargs["suppress_compatibility_warning"] is True
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


# --------------------------------------------------------------------------- #
# entrypoint wiring
# --------------------------------------------------------------------------- #


class _FakeAmdSession:
    """AgentSession stand-in: event registration, start(), and say()."""

    def __init__(self) -> None:
        self.say_calls: list[str] = []
        self.started = False

    def on(self, _event: str):
        def _register(fn):
            return fn

        return _register

    async def start(self, **_kwargs: object) -> None:
        self.started = True

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
    def __init__(self, name: str) -> None:
        self.name = name
        self.remote_participants: dict[str, object] = {}
        self.local_participant = _FakeLocalParticipant()

    def on(self, _event: str):
        def _register(fn):
            return fn

        return _register


class _FakeAmdCtx:
    def __init__(self, metadata: str, room_name: str) -> None:
        self.job = SimpleNamespace(metadata=metadata)
        self.room = _FakeAmdRoom(room_name)
        self.proc = SimpleNamespace(userdata={"vad": object()})
        self.shutdown_calls: list[str] = []
        self.delete_room_calls = 0
        self.shutdown_callbacks: list[Any] = []

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
) -> tuple[UUID, _FakeAmdCtx, _FakeAmdSession]:
    """Run ``entrypoint`` with AMD stubbed, then fire the shutdown callback."""
    from hailhq.voicebot import agent as agent_mod

    org_id = UUID("11111111-2222-3333-4444-555555555555")
    call_id = await _make_call_row(async_session)
    session = _FakeAmdSession()

    async def _no_org_cfgs(_org_id: UUID | None, *, skip_llm: bool = False) -> dict:
        return {}

    async def _fake_run_amd(_session: object, _call_id: UUID):
        return amd_result

    monkeypatch.setattr(agent_mod, "resolve_org_configs", _no_org_cfgs)
    monkeypatch.setattr(agent_mod, "build_session", lambda *a, **k: session)
    monkeypatch.setattr(agent_mod, "run_amd", _fake_run_amd)
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
    )

    await entrypoint_under_test(ctx)

    # The job releases after entrypoint returns; run the callback the same
    # way the SDK would so on_call_end lands.
    for cb in ctx.shutdown_callbacks:
        await cb()
    await asyncio.sleep(0)
    return call_id, ctx, session


def entrypoint_under_test(ctx: Any):
    from hailhq.voicebot.agent import entrypoint

    return entrypoint(ctx)


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


@pytest.mark.parametrize("category", ["human", "uncertain", "machine-ivr"])
async def test_non_hangup_categories_speak_the_disclosure(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
) -> None:
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=_prediction(category)
    )

    assert session.say_calls == [AI_DISCLOSURE_LINE, "Is this a good time?"]
    assert ctx.delete_room_calls == 0
    assert ctx.shutdown_calls == []


async def test_detection_failure_proceeds_like_a_human_answer(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.voicebot.agent import AI_DISCLOSURE_LINE

    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=None
    )

    assert session.say_calls[0] == AI_DISCLOSURE_LINE
    assert ctx.delete_room_calls == 0


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
    }


async def test_amd_result_event_written_when_detection_failed(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_id, _ctx, _session = await _drive_entrypoint(
        async_session, monkeypatch, amd_result=None
    )

    events = await _amd_events(async_session, call_id)
    assert len(events) == 1
    assert events[0].payload == {"category": None, "transcript": None}
