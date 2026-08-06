"""Give up on a persistently failing mode B (per-call BYO) LLM endpoint.

Two layers:

* Unit tests on :func:`arm_byo_llm_giveup`, driven with **real**
  ``livekit.agents`` event objects (``ErrorEvent`` wrapping ``LLMError`` /
  ``STTError``, ``ConversationItemAddedEvent`` wrapping ``ChatMessage``) so
  the ``isinstance`` / ``recoverable`` / ``role`` checks are exercised against
  the installed 1.6.6 shapes rather than duck-typed stand-ins.
* Entrypoint tests proving the *arming condition*: only a call carrying a
  per-call ``llm`` block (mode B) arms this. Mode A (house chain) and mode C
  (org BYO) must be inert — they have their own failure semantics.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import Call
from hailhq.voicebot import agent as agent_mod
from hailhq.voicebot.agent import (
    BYO_LLM_FAILURE_ANNOUNCEMENT,
    BYO_LLM_FAILURE_END_REASON,
    MAX_CONSECUTIVE_LLM_ERRORS,
    arm_byo_llm_giveup,
    entrypoint,
)
from livekit.agents.llm import ChatMessage, LLMError
from livekit.agents.stt import STTError
from livekit.agents.voice.events import ConversationItemAddedEvent, ErrorEvent
from sqlalchemy.ext.asyncio import AsyncSession

from ._fakes import FakeJobContext, FakeSpeechHandle
from .test_agent import _make_call_row

ORG_ID = UUID("11111111-2222-3333-4444-555555555555")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _EmittingSession:
    """AgentSession stand-in with EventEmitter fan-out and a recording say().

    ``AgentSession`` inherits ``rtc.EventEmitter``, which keeps *every*
    registered handler for an event name — ``attach_event_handlers`` and
    ``arm_byo_llm_giveup`` both listen on ``error`` and
    ``conversation_item_added``. A dict-of-lists mirrors that; a
    single-handler fake would silently hide one of them.
    """

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.say_calls: list[tuple[str, bool]] = []
        self.last_handle: FakeSpeechHandle | None = None
        self.replies: list[str | None] = []
        self.started = False

    def on(self, event: str):
        def _register(fn):
            self.handlers.setdefault(event, []).append(fn)
            return fn

        return _register

    def emit(self, event: str, ev: Any) -> None:
        for fn in list(self.handlers.get(event, [])):
            fn(ev)

    def say(self, text: str, *, allow_interruptions: bool = True) -> FakeSpeechHandle:
        self.say_calls.append((text, allow_interruptions))
        handle = FakeSpeechHandle()
        self.last_handle = handle
        return handle

    def generate_reply(self, *, instructions: str | None = None) -> Any:
        self.replies.append(instructions)
        return SimpleNamespace()

    async def start(self, **_kwargs: object) -> None:
        self.started = True


class _EntrypointCtx:
    """Enough of JobContext to drive ``entrypoint`` past the give-up wiring."""

    def __init__(self, metadata: str, room_name: str) -> None:
        self.job = SimpleNamespace(metadata=metadata)
        self.room = _EntrypointRoom(room_name)
        self.proc = SimpleNamespace(userdata={"vad": object()})
        self.shutdown_calls: list[str] = []
        self.shutdown_callbacks: list[Any] = []
        self.delete_room_calls = 0

    async def connect(self) -> None:
        return None

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)

    async def delete_room(self) -> None:
        self.delete_room_calls += 1

    def add_shutdown_callback(self, cb: Any) -> None:
        self.shutdown_callbacks.append(cb)


class _EntrypointRoom:
    def __init__(self, name: str) -> None:
        self.name = name
        self.remote_participants: dict[str, object] = {}
        self.local_participant = SimpleNamespace()

    def on(self, _event: str):
        def _register(fn):
            return fn

        return _register


def _llm_error(*, recoverable: bool = False) -> ErrorEvent:
    """A real session ``error`` event carrying a real ``LLMError``."""
    return ErrorEvent(
        error=LLMError(
            timestamp=0.0,
            label="openai.LLM",
            error=RuntimeError("byo endpoint returned 500"),
            recoverable=recoverable,
        ),
        source=None,
    )


def _stt_error() -> ErrorEvent:
    return ErrorEvent(
        error=STTError(
            timestamp=0.0,
            label="deepgram.STT",
            error=RuntimeError("stt died"),
            recoverable=False,
        ),
        source=None,
    )


def _assistant_turn() -> ConversationItemAddedEvent:
    return ConversationItemAddedEvent(
        item=ChatMessage(role="assistant", content=["Sure, I can help with that."])
    )


def _user_turn() -> ConversationItemAddedEvent:
    return ConversationItemAddedEvent(item=ChatMessage(role="user", content=["Hello?"]))


async def _drain(tasks: set[asyncio.Task[None]]) -> None:
    """Await the give-up task the (sync) event handler spawned."""
    while tasks:
        await asyncio.gather(*list(tasks), return_exceptions=True)


def _arm(
    ctx: FakeJobContext, session: _EmittingSession
) -> tuple[set[asyncio.Task[None]], dict[str, bool]]:
    """Arm the give-up on ``session``; returns its task set and a fire record.

    ``on_fire`` asserts, at the moment it runs, that ``ctx.shutdown`` has not
    been called yet — the ordering ``soft_cap_announce_and_hangup`` mandates
    (end_reason must be stamped BEFORE shutdown, or ``_on_session_close``
    would map the bare ``job_shutdown`` to ``worker_shutdown``/``failed``).
    """
    tasks: set[asyncio.Task[None]] = set()
    fired = {"value": False}

    def _on_fire() -> None:
        assert ctx.shutdown_calls == [], "end_reason must be stamped before shutdown"
        fired["value"] = True

    arm_byo_llm_giveup(
        ctx,  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        UUID("22222222-3333-4444-5555-666666666666"),
        tasks,
        on_fire=_on_fire,
    )
    return tasks, fired


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


async def test_two_failures_then_a_success_resets_the_counter() -> None:
    """A turn that completed is proof the endpoint is alive: start over.

    Two failures, an assistant turn, then two more must not hang up — the
    tolerance is *consecutive* failures, not cumulative ones.
    """
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, fired = _arm(ctx, session)

    session.emit("error", _llm_error())
    session.emit("error", _llm_error())
    session.emit("conversation_item_added", _assistant_turn())
    session.emit("error", _llm_error())
    session.emit("error", _llm_error())
    await _drain(tasks)

    assert fired["value"] is False
    assert session.say_calls == []
    assert ctx.shutdown_calls == []


async def test_three_consecutive_errors_hangs_up_exactly_once() -> None:
    """The goodbye is spoken uninterruptibly, then the call ends."""
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, fired = _arm(ctx, session)

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS):
        session.emit("error", _llm_error())
    await _drain(tasks)

    assert session.say_calls == [(BYO_LLM_FAILURE_ANNOUNCEMENT, False)]
    # The caller must actually hear the goodbye before the line drops.
    assert session.last_handle is not None
    assert session.last_handle.played_out is True
    assert fired["value"] is True
    assert ctx.shutdown_calls == [BYO_LLM_FAILURE_END_REASON]
    assert BYO_LLM_FAILURE_END_REASON == CallEndReason.LLM_ENDPOINT_FAILED.value


async def test_a_fourth_error_after_the_hangup_does_not_fire_again() -> None:
    """The latch is set inside the handler, so an error arriving while the
    goodbye is still playing cannot queue a second hangup."""
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, _fired = _arm(ctx, session)

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS):
        session.emit("error", _llm_error())
    # Fires before the give-up task has had a chance to run, mirroring a
    # fourth failure landing while the goodbye plays.
    session.emit("error", _llm_error())
    await _drain(tasks)
    session.emit("error", _llm_error())
    await _drain(tasks)

    assert len(session.say_calls) == 1
    assert ctx.shutdown_calls == [BYO_LLM_FAILURE_END_REASON]


async def test_recoverable_errors_never_count() -> None:
    """``recoverable=True`` is the plugin's own retry ladder, not a verdict."""
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, fired = _arm(ctx, session)

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS * 3):
        session.emit("error", _llm_error(recoverable=True))
    await _drain(tasks)

    assert fired["value"] is False
    assert ctx.shutdown_calls == []


async def test_non_llm_errors_never_count() -> None:
    """``ErrorEvent.error`` is a union and STT/TTS errors carry ``recoverable``
    too — a dying STT must not be charged to the caller's LLM endpoint."""
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, fired = _arm(ctx, session)

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS):
        session.emit("error", _stt_error())
    await _drain(tasks)

    assert fired["value"] is False
    assert ctx.shutdown_calls == []


async def test_only_assistant_items_reset_the_counter() -> None:
    """A user transcript proves the caller is talking, not that the brain
    answered — it must not clear the failure streak."""
    ctx = FakeJobContext()
    session = _EmittingSession()
    tasks, _fired = _arm(ctx, session)

    session.emit("error", _llm_error())
    session.emit("conversation_item_added", _user_turn())
    session.emit("error", _llm_error())
    session.emit("conversation_item_added", _user_turn())
    session.emit("error", _llm_error())
    await _drain(tasks)

    assert ctx.shutdown_calls == [BYO_LLM_FAILURE_END_REASON]


# --------------------------------------------------------------------------- #
# Arming condition — mode B only
# --------------------------------------------------------------------------- #


async def _drive_entrypoint(
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    llm_block: dict[str, str] | None,
    org_llm_configured: bool,
) -> tuple[UUID, _EntrypointCtx, _EmittingSession]:
    """Run the real ``entrypoint`` with providers stubbed out.

    ``build_session`` is patched (no real provider construction) but every
    handler-wiring line in ``entrypoint`` — including the mode-B gate under
    test — runs for real.
    """
    call_id = await _make_call_row(async_session)
    session = _EmittingSession()

    async def _org_cfgs(_org_id: UUID | None, *, skip_llm: bool = False) -> dict:
        # Mode C: the org has a standing BYO llm row. `skip_llm` is what
        # entrypoint passes when a per-call block already won precedence.
        if org_llm_configured and not skip_llm:
            return {"llm": object()}
        return {}

    async def _no_amd(_session: object, _call_id: UUID) -> None:
        return None

    monkeypatch.setattr(agent_mod, "resolve_org_configs", _org_cfgs)
    monkeypatch.setattr(agent_mod, "build_session", lambda *a, **k: session)
    monkeypatch.setattr(agent_mod, "run_amd", _no_amd)
    # The per-call base_url is re-resolved off the event loop at call time;
    # keep the test off the network.
    monkeypatch.setattr(agent_mod, "assert_public_https_url", lambda url: url)
    monkeypatch.setattr(
        agent_mod.settings, "hail_voice_max_duration_seconds", 0, raising=False
    )

    metadata: dict[str, Any] = {
        "call_id": str(call_id),
        "organization_id": str(ORG_ID),
        "tools": [],
    }
    if llm_block is not None:
        metadata["llm"] = llm_block

    ctx = _EntrypointCtx(json.dumps(metadata), room_name=f"hail-{call_id}")
    await entrypoint(ctx)  # type: ignore[arg-type]
    return call_id, ctx, session


async def test_mode_b_call_arms_the_giveup(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-call ``llm`` block ends the call after three failed turns.

    Also drives the job's shutdown callback the way the SDK would, so the
    stamped ``captured`` values are proven to reach the Call row — the part
    the API consumer actually sees.
    """
    call_id, ctx, session = await _drive_entrypoint(
        async_session,
        monkeypatch,
        llm_block={
            "base_url": "https://brain.example.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
        },
        org_llm_configured=False,
    )

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS):
        session.emit("error", _llm_error())
    for _ in range(5):
        await asyncio.sleep(0)

    assert (BYO_LLM_FAILURE_ANNOUNCEMENT, False) in session.say_calls
    assert ctx.shutdown_calls == [BYO_LLM_FAILURE_END_REASON]

    for cb in ctx.shutdown_callbacks:
        await cb()
    call = await async_session.get(Call, call_id)
    assert call is not None
    await async_session.refresh(call)
    assert call.status == "failed"
    assert call.end_reason == CallEndReason.LLM_ENDPOINT_FAILED.value


async def test_mode_a_call_does_not_arm_the_giveup(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No per-call ``llm``, no org row: the house FallbackAdapter owns
    recovery, so this guardrail must be inert."""
    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, llm_block=None, org_llm_configured=False
    )

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS * 2):
        session.emit("error", _llm_error())
    for _ in range(5):
        await asyncio.sleep(0)

    assert BYO_LLM_FAILURE_ANNOUNCEMENT not in [t for t, _ in session.say_calls]
    assert ctx.shutdown_calls == []


async def test_mode_c_call_does_not_arm_the_giveup(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standing org BYO either falls back (``fallback_enabled``) or already
    fails fast at session build as ``provider_key_error`` — the org owns that
    config, so Hail does not add a second give-up on top of it."""
    _call_id, ctx, session = await _drive_entrypoint(
        async_session, monkeypatch, llm_block=None, org_llm_configured=True
    )

    for _ in range(MAX_CONSECUTIVE_LLM_ERRORS * 2):
        session.emit("error", _llm_error())
    for _ in range(5):
        await asyncio.sleep(0)

    assert BYO_LLM_FAILURE_ANNOUNCEMENT not in [t for t, _ in session.say_calls]
    assert ctx.shutdown_calls == []
