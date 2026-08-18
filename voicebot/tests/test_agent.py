"""Tests for ``hailhq.voicebot.agent``.

Mix of pure unit (parse_metadata, on_call_end) and behavioral
(``AgentSession.run(user_input=...)``) per the LiveKit Agents skill: voice-agent
behavior is code, so behavioral coverage is mandatory.

The behavioral test runs in **text mode** (no audio in/out), which lets us
skip VAD/STT/TTS entirely — verified 2026-04-28 against
``docs.livekit.io/agents/build/testing/``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from cryptography.fernet import InvalidToken
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import Call, CallEvent, PhoneNumber, UsageEvent
from hailhq.core.pool import CALL_META_FROM_POOL
from hailhq.voicebot.agent import (
    AI_DISCLOSURE_LINE,
    SIP_CALL_STATUS_ACTIVE,
    SIP_CALL_STATUS_ATTRIBUTE,
    SOFT_CAP_ANNOUNCEMENT,
    SOFT_CAP_END_REASON,
    VOICE_PREAMBLE,
    _sanitize_tts_stream,
    attach_event_handlers,
    build_instructions,
    build_tools_safely,
    disconnect_reason_to_status,
    entrypoint,
    is_sip_answer_signal,
    make_agent_hangup,
    make_agent_send_dtmf,
    mark_call_answered,
    on_call_end,
    opening_instructions,
    parse_metadata,
    soft_cap_announce_and_hangup,
    speak_greeting,
    speech_text,
)
from livekit import rtc
from livekit.agents import Agent, AgentSession
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ._fakes import FakeAnnouncingSession, FakeJobContext, FakeLLM


def test_metadata_parser_handles_missing_optional_fields() -> None:
    """Optional fields default cleanly; only ``call_id`` is required."""
    raw_min = '{"call_id": "11111111-1111-1111-1111-111111111111"}'
    parsed = parse_metadata(raw_min)
    assert parsed["call_id"] == UUID("11111111-1111-1111-1111-111111111111")
    assert parsed.get("system_prompt") is None
    assert parsed.get("llm") is None
    assert parsed.get("first_message") is None


def test_metadata_parser_rejects_missing_call_id() -> None:
    with pytest.raises(ValueError, match="call_id"):
        parse_metadata("{}")


def test_metadata_parser_rejects_empty_string() -> None:
    """Empty/None metadata is treated as ``{}`` -> missing ``call_id``."""
    with pytest.raises(ValueError, match="call_id"):
        parse_metadata(None)


# --------------------------------------------------------------------------- #
# Voice-call instruction assembly (Bug 2 — agent must not claim to be text-based)
# --------------------------------------------------------------------------- #


def test_build_instructions_prepends_preamble_to_caller_prompt() -> None:
    """The fixed voice preamble always leads; the caller prompt follows it."""
    caller = "You are calling Dr. Lee's office to book a teeth cleaning."
    out = build_instructions(caller)

    assert out.startswith(VOICE_PREAMBLE)
    assert caller in out
    # Preamble precedes the caller prompt — it frames, never gets replaced.
    assert out.index(VOICE_PREAMBLE) < out.index(caller)


def test_build_instructions_present_without_caller_prompt() -> None:
    """A missing/empty caller prompt still yields the voice framing.

    This is the exact condition behind the bug: no caller framing → the model
    defaulted to a generic chat-assistant self-concept.
    """
    assert build_instructions(None) == VOICE_PREAMBLE
    assert build_instructions("") == VOICE_PREAMBLE
    assert VOICE_PREAMBLE.strip(), "preamble must be non-empty"


def test_build_instructions_adds_caller_boundary_header() -> None:
    """A caller prompt is appended under its own header so its Markdown
    sections never collide with the preamble's `#` sections."""
    caller = "You are calling Dr. Lee's office to book a teeth cleaning."
    out = build_instructions(caller)

    assert "# Caller instructions" in out
    assert out.index("# Caller instructions") < out.index(caller)


def test_build_instructions_composes_for_a_mode_b_call_with_a_prompt() -> None:
    """A mode B dispatch (BYO ``llm``) that also carries a ``system_prompt``
    gets the same composition as mode A: preamble first, caller prompt after.

    This is the combination `POST /calls` now accepts — nothing here is
    mode-aware, which is why no voicebot change was needed for it.
    """
    caller = "You are calling Dr. Lee's office to book a teeth cleaning."
    raw = json.dumps(
        {
            "call_id": "11111111-1111-1111-1111-111111111111",
            "system_prompt": caller,
            "llm": {
                "base_url": "https://byo.example.com/v1",
                "api_key": "k",
                "model": "m",
            },
        }
    )
    metadata = parse_metadata(raw)
    assert metadata["llm"]["model"] == "m"  # this really is a mode B call

    out = build_instructions(metadata.get("system_prompt"))

    assert out.startswith(VOICE_PREAMBLE)
    assert caller in out
    assert out.index(VOICE_PREAMBLE) < out.index(caller)


def test_voice_preamble_frames_the_channel() -> None:
    """The preamble explicitly addresses the observed failure modes."""
    low = VOICE_PREAMBLE.lower()
    # Forbids the exact phrasing the agent used on the bad call.
    assert "text-based" in low
    # Establishes the telephone/voice channel.
    assert "telephone" in low or "phone call" in low
    assert "speech-to-text" in low and "text-to-speech" in low
    # Never claim to be human.
    assert "human" in low
    # No emoji — the real TTS fix (stripping the stored transcript would not
    # change what the LLM hands the TTS engine).
    assert "emoji" in low


def test_voice_preamble_requires_confirmation_before_sending() -> None:
    """Guardrails must require the agent to confirm before sending a message.

    Regression for an agent tool (send_sms/send_email) firing without the
    other party ever agreeing to what would be sent.
    """
    lowered = VOICE_PREAMBLE.lower()
    assert "before sending" in lowered
    assert "confirmation" in lowered


class _FakeSession:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def on(self, event_name: str):
        def _register(fn):
            self.handlers[event_name] = fn
            return fn

        return _register


async def _make_call_row(session: AsyncSession) -> UUID:
    """Insert a phone_number + queued call against a synthetic org_id."""
    org_id = UUID("11111111-2222-3333-4444-555555555555")

    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    call = Call(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "cartesia"},
        status="dialing",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call.id


class _FakeRoom:
    """Minimal ctx.room: entrypoint registers event handlers and scans
    current participants before the provider-config resolve we're testing."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.remote_participants: dict[str, object] = {}

    def on(self, _event: str):
        def _register(fn):
            return fn

        return _register


class _FakeEntrypointCtx:
    """Enough of JobContext to drive entrypoint() to the org-config resolve.

    entrypoint reads ctx.job.metadata, awaits ctx.connect(), wires
    ctx.room.on(...), scans ctx.room.remote_participants, reads
    ctx.proc.userdata['vad'], and on the provider_key_error path calls
    ctx.shutdown(reason=...). We record shutdown reasons to assert the
    clean fail-fast path ran.
    """

    def __init__(self, metadata: str, room_name: str) -> None:
        self.job = SimpleNamespace(metadata=metadata)
        self.room = _FakeRoom(room_name)
        self.proc = SimpleNamespace(userdata={"vad": object()})
        self.shutdown_calls: list[str] = []

    async def connect(self) -> None:
        return None

    def shutdown(self, reason: str = "") -> None:
        self.shutdown_calls.append(reason)


async def test_entrypoint_org_config_load_failure_finalizes_cleanly(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decrypt failure loading the org's BYO config (the real trigger being
    a HAIL_PROVIDER_SECRET_KEY rotation invalidating stored ciphertext) must
    fail fast as provider_key_error — finalizing the Call row via on_call_end
    — and must NOT propagate raw out of entrypoint() and leak the number."""
    from hailhq.voicebot import agent as agent_mod

    org_id = UUID("11111111-2222-3333-4444-555555555555")
    call_id = await _make_call_row(async_session)

    async def _boom(_org_id: UUID | None, *, skip_llm: bool = False) -> dict:
        raise InvalidToken("stale ciphertext after key rotation")

    monkeypatch.setattr(agent_mod, "resolve_org_configs", _boom)

    ctx = _FakeEntrypointCtx(
        metadata=json.dumps({"call_id": str(call_id), "organization_id": str(org_id)}),
        room_name=f"hail-{call_id}",
    )

    # Returns cleanly (the InvalidToken is converted, not propagated).
    await entrypoint(ctx)  # type: ignore[arg-type]

    assert ctx.shutdown_calls == ["provider_key_error"]

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "provider_key_error"


async def test_entrypoint_build_session_type_error_finalizes_cleanly(
    async_session: AsyncSession,
) -> None:
    """A malformed ``voice_config.language`` (a dict instead of a string
    code — e.g. a direct LiveKit dispatch with hand-crafted metadata) makes
    ``build_session``'s ``SUPPORTED_LANGUAGES`` membership check raise
    ``TypeError`` (unhashable type). That must fail fast as
    provider_key_error too, not escape ``entrypoint()`` raw and leak the pool
    number — same leak class as the InvalidToken case above, just raised
    from inside build_session (called within the same guarded block) instead
    of from resolve_org_configs."""
    call_id = await _make_call_row(async_session)

    ctx = _FakeEntrypointCtx(
        metadata=json.dumps(
            {
                "call_id": str(call_id),
                "voice_config": {"language": {"unexpected": "shape"}},
            }
        ),
        room_name=f"hail-{call_id}",
    )

    # Returns cleanly (the TypeError is converted, not propagated).
    await entrypoint(ctx)  # type: ignore[arg-type]

    assert ctx.shutdown_calls == ["provider_key_error"]

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "provider_key_error"


async def test_agent_session_run_emits_assistant_message() -> None:
    """Behavioral: one user turn yields at least one assistant message.

    Text-mode ``run(user_input=...)`` skips STT/TTS, so we only need an
    LLM. Asserts both: (1) the system_prompt from metadata is wired into the
    Agent's ``instructions``, and (2) the assistant produces a reply.
    """
    instructions = "You are Hail, a helpful agent."
    fake_llm = FakeLLM(reply="ack: hello back")

    async with AgentSession(llm=fake_llm) as session:
        agent = Agent(instructions=instructions)
        await session.start(agent=agent)
        result = await session.run(user_input="hello")

        assert agent.instructions == instructions
        result.expect[:].contains_message(role="assistant")


async def test_attach_event_handlers_ignores_non_message_items() -> None:
    session = _FakeSession()
    call_id = UUID("11111111-1111-1111-1111-111111111111")

    event_tasks = attach_event_handlers(session, call_id)
    on_item = session.handlers["conversation_item_added"]

    on_item(SimpleNamespace(item=SimpleNamespace()))

    assert not event_tasks


async def test_call_event_written_for_user_turn(async_session: AsyncSession) -> None:
    """Behavioral DB test: one round-trip writes a ``user_turn`` row.

    The ``async_session`` fixture installs the test sessionmaker into
    ``hailhq.core.db._sessionmaker`` so the production
    :func:`session_scope` used by ``write_call_event`` writes to the test
    database transparently.
    """
    call_id = await _make_call_row(async_session)
    fake_llm = FakeLLM(reply="ack")

    async with AgentSession(llm=fake_llm) as session:
        event_tasks = attach_event_handlers(session, call_id)
        agent = Agent(instructions="test")
        await session.start(agent=agent)
        await session.run(user_input="hello")

    # Drain pending row writes; handlers schedule via asyncio.ensure_future.
    if event_tasks:
        await asyncio.gather(*list(event_tasks), return_exceptions=True)

    rows = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "user_turn"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "expected a user_turn call_events row to be written"
    payload = rows[0].payload
    assert payload.get("role") == "user"
    assert "hello" in (payload.get("text") or "")


async def test_on_call_end_marks_call_completed(async_session: AsyncSession) -> None:
    """``on_call_end`` finalizes the row: status=completed + ended_at set."""
    call_id = await _make_call_row(async_session)
    before = datetime.now(timezone.utc)

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    # Re-read in a fresh select (the row was updated in another session).
    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    # Avoid stale identity-map data.
    await async_session.refresh(refreshed)
    assert refreshed.status == "completed"
    assert refreshed.ended_at is not None
    assert refreshed.ended_at >= before
    # v1 stub: recording_s3_key stays None.
    assert refreshed.recording_s3_key is None


async def test_on_call_end_inserts_usage_event(async_session: AsyncSession) -> None:
    """A call with a non-zero duration writes one ``voice`` usage_events row.

    The voicebot no longer does money math; it just records raw duration in ms.
    The website's private rater turns that into a dollar debit later.
    """
    call_id = await _make_call_row(async_session)
    # Backdate the pickup so the (now - answered_at) delta is roughly 60s.
    # `answered_at` is what gates billing: it means the SIP leg went active.
    answered = datetime.now(timezone.utc) - timedelta(seconds=60)
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=answered, answered_at=answered)
    )
    await async_session.commit()

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    # cost_cents stays NULL — the API service no longer prices.
    assert refreshed.cost_cents is None

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(
                    UsageEvent.organization_id == refreshed.organization_id,
                    UsageEvent.channel == "voice",
                    UsageEvent.ref == str(call_id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1, f"expected exactly one usage_events row, got {len(rows)}"
    event = rows[0]
    # ~60s = 60_000ms, allow some wiggle for execution time between start backdate and on_call_end.
    assert 55_000 <= event.units <= 65_000
    assert event.priced_at is None


async def test_on_call_end_no_usage_event_for_zero_duration(
    async_session: AsyncSession,
) -> None:
    """A call that never started (no ``started_at``) writes no usage_events row."""
    call_id = await _make_call_row(async_session)

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    # No started_at, no recording_duration_ms → duration_ms = 0 → no row written.
    assert refreshed.cost_cents is None

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(
                    UsageEvent.organization_id == refreshed.organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_on_call_end_releases_pool_reservation(
    async_session: AsyncSession,
) -> None:
    """Calls that used a pool number release their reservation on completion."""
    # Build a pool number + a Call that holds its reservation.
    pool_pn = PhoneNumber(
        organization_id=None,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_pool",
        provisioning_state="active",
        is_pool=True,
    )
    async_session.add(pool_pn)
    await async_session.flush()

    org_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    call = Call(
        organization_id=org_id,
        from_number_id=pool_pn.id,
        from_e164=pool_pn.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "cartesia"},
        status="dialing",
        metadata_={CALL_META_FROM_POOL: True},
    )
    async_session.add(call)
    await async_session.flush()
    pool_pn.reserved_call_id = call.id
    await async_session.commit()

    await on_call_end(call.id, room_name=f"hail-{call.id}")

    await async_session.refresh(pool_pn)
    assert pool_pn.reserved_call_id is None  # released by finalize_call

    refreshed_call = (
        await async_session.execute(select(Call).where(Call.id == call.id))
    ).scalar_one()
    await async_session.refresh(refreshed_call)
    assert refreshed_call.status == "completed"


# --------------------------------------------------------------------------- #
# SIP DisconnectReason → Call.status mapping
# --------------------------------------------------------------------------- #


# Pure unit tests for the mapping helper — no DB, no session. Parametrized
# against the real `rtc.DisconnectReason` enum from the installed SDK so a
# protobuf rename upstream surfaces immediately in CI.


@pytest.mark.parametrize(
    "reason,expected",
    [
        (rtc.DisconnectReason.USER_UNAVAILABLE, ("no_answer", "user_unavailable")),
        (rtc.DisconnectReason.USER_REJECTED, ("busy", "user_rejected")),
        (rtc.DisconnectReason.SIP_TRUNK_FAILURE, ("failed", "sip_trunk_failure")),
        (rtc.DisconnectReason.CONNECTION_TIMEOUT, ("failed", "connection_timeout")),
        (rtc.DisconnectReason.MEDIA_FAILURE, ("failed", "media_failure")),
        # CLIENT_INITIATED is the normal-hangup happy path — leave status as-is.
        (rtc.DisconnectReason.CLIENT_INITIATED, (None, None)),
        # A missing disconnect_reason must not override the default status.
        (None, (None, None)),
    ],
)
def test_disconnect_reason_mapping(
    reason: int | None, expected: tuple[str | None, str | None]
) -> None:
    assert disconnect_reason_to_status(reason) == expected


# --------------------------------------------------------------------------- #
# SIP answer signal → in_progress + answered_at (Bug 1a)
# --------------------------------------------------------------------------- #


def _fake_participant(kind: int, call_status: str | None) -> SimpleNamespace:
    attrs = {} if call_status is None else {SIP_CALL_STATUS_ATTRIBUTE: call_status}
    return SimpleNamespace(kind=kind, attributes=attrs)


@pytest.mark.parametrize(
    "kind,call_status,expected",
    [
        # SIP participant whose call went active = the callee answered.
        (rtc.ParticipantKind.PARTICIPANT_KIND_SIP, SIP_CALL_STATUS_ACTIVE, True),
        # Still dialing — not answered yet.
        (rtc.ParticipantKind.PARTICIPANT_KIND_SIP, "dialing", False),
        # No status attribute yet.
        (rtc.ParticipantKind.PARTICIPANT_KIND_SIP, None, False),
        # The agent's own participant going active is not a callee answer.
        (rtc.ParticipantKind.PARTICIPANT_KIND_AGENT, SIP_CALL_STATUS_ACTIVE, False),
        (rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD, SIP_CALL_STATUS_ACTIVE, False),
    ],
)
def test_is_sip_answer_signal(
    kind: int, call_status: str | None, expected: bool
) -> None:
    assert is_sip_answer_signal(_fake_participant(kind, call_status)) is expected


async def test_mark_call_answered_transitions_to_in_progress(
    async_session: AsyncSession,
) -> None:
    """Pickup flips dialing → in_progress, stamps answered_at, logs the event."""
    call_id = await _make_call_row(async_session)  # status='dialing'
    before = datetime.now(timezone.utc)

    await mark_call_answered(call_id)

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "in_progress"
    assert refreshed.answered_at is not None
    assert refreshed.answered_at >= before

    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {"from": "dialing", "to": "in_progress"}


async def test_mark_call_answered_is_idempotent(async_session: AsyncSession) -> None:
    """A duplicate attribute event must not re-stamp or double-log."""
    call_id = await _make_call_row(async_session)

    await mark_call_answered(call_id)
    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    first_answered_at = refreshed.answered_at

    await mark_call_answered(call_id)
    refreshed2 = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed2)
    assert refreshed2.status == "in_progress"
    assert refreshed2.answered_at == first_answered_at  # unchanged

    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1, "second answer must not write a second state_change"


async def test_mark_call_answered_returns_false_when_no_transition(
    async_session: AsyncSession,
) -> None:
    """The return value reports whether a row actually transitioned.

    The entrypoint latches its dedupe flag on a True return only, so a second
    (already-answered) signal must report False.
    """
    call_id = await _make_call_row(async_session)
    assert await mark_call_answered(call_id) is True
    assert await mark_call_answered(call_id) is False  # already in_progress


async def test_on_call_end_noops_when_reconciler_already_closed(
    async_session: AsyncSession,
) -> None:
    """A late on_call_end after the sweeper force-closed a call must not clobber.

    Regression for the unguarded terminal write: an un-hung worker running
    on_call_end after sweep_stale_calls failed the row must NOT overwrite the
    outcome back to 'completed', must NOT emit a second contradictory
    state_change, and must NOT write a (duplicate) usage_events row —
    usage_events.ref carries no unique constraint, so nothing else stops it.
    """
    call_id = await _make_call_row(async_session)
    # The call had been answered (so on_call_end would otherwise bill it)...
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(
            status="in_progress",
            answered_at=datetime.now(timezone.utc) - timedelta(seconds=90),
        )
    )
    await async_session.commit()
    # ...then the reconciler force-closed it (as sweep_stale_calls would).
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(
            status="failed",
            end_reason="sweeper_timeout",
            ended_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    # Outcome preserved — not reverted to completed/normal_hangup.
    assert refreshed.status == "failed"
    assert refreshed.end_reason == "sweeper_timeout"

    # No state_change emitted by on_call_end (it no-oped).
    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == []

    # No spurious bill for a call the reconciler already failed.
    usage = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert usage == []


async def test_mark_call_answered_noops_on_terminal_call(
    async_session: AsyncSession,
) -> None:
    """A late answer signal must never resurrect an already-closed call."""
    call_id = await _make_call_row(async_session)
    # Race: on_call_end already wrote a terminal status (e.g. fast hangup).
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(status="completed", end_reason="normal_hangup", answered_at=None)
    )
    await async_session.commit()

    await mark_call_answered(call_id)

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "completed"  # not clobbered back to in_progress
    assert refreshed.answered_at is None

    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == []


async def test_call_lifecycle_dialing_to_completed_integration(
    async_session: AsyncSession,
) -> None:
    """End-to-end walk: dialing → in_progress (pickup) → completed (hangup).

    The spec's headline test: a completed call must pass through ``in_progress``
    and carry a non-null ``answered_at``. Drives the real functions in sequence
    (no live room needed) and asserts ``answered_at`` is stamped on pickup AND
    survives ``on_call_end`` — plus the full state_change trail, the regression
    that previously left finished calls looking stuck at 'dialing'.
    """
    call_id = await _make_call_row(async_session)  # status='dialing'

    # Pickup.
    await mark_call_answered(call_id)
    mid = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(mid)
    assert mid.status == "in_progress"
    assert mid.answered_at is not None
    answered_at = mid.answered_at

    # Hangup.
    await on_call_end(call_id, room_name=f"hail-{call_id}")
    final = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(final)
    assert final.status == "completed"
    # answered_at must not be wiped by the terminal write.
    assert final.answered_at == answered_at

    events = (
        (
            await async_session.execute(
                select(CallEvent)
                .where(CallEvent.call_id == call_id, CallEvent.kind == "state_change")
                .order_by(CallEvent.occurred_at)
            )
        )
        .scalars()
        .all()
    )
    trail = [(e.payload["from"], e.payload["to"]) for e in events]
    assert trail == [
        ("dialing", "in_progress"),
        ("in_progress", "completed"),
    ]


# --------------------------------------------------------------------------- #
# on_call_end overrides (integration with the calls table)
# --------------------------------------------------------------------------- #


async def test_on_call_end_writes_status_override(
    async_session: AsyncSession,
) -> None:
    """`status_override` and `end_reason_override` land on the Call row."""
    call_id = await _make_call_row(async_session)

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="no_answer",
        end_reason_override="user_unavailable",
    )

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "no_answer"
    assert refreshed.end_reason == "user_unavailable"
    assert refreshed.ended_at is not None


async def test_on_call_end_no_usage_event_for_overridden_status(
    async_session: AsyncSession,
) -> None:
    """Non-completed terminal statuses are not billed even if the call rang."""
    call_id = await _make_call_row(async_session)
    # Backdate started_at so a duration would otherwise be billed.
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=datetime.now(timezone.utc) - timedelta(seconds=45))
    )
    await async_session.commit()

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="busy",
        end_reason_override="user_rejected",
    )

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "busy"

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert rows == [], "non-completed call should not write a usage row"


async def test_on_call_end_emits_terminal_state_change_event(
    async_session: AsyncSession,
) -> None:
    """on_call_end logs a state_change so observers see the terminal transition.

    Without this, the only state_change ever emitted is queued→dialing (from
    the API) — the symptom that made completed calls look stuck at 'dialing'.
    """
    call_id = await _make_call_row(async_session)  # status='dialing'
    # Simulate a real conversation: the call had reached in_progress.
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(status="in_progress", answered_at=datetime.now(timezone.utc))
    )
    await async_session.commit()

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {
        "from": "in_progress",
        "to": "completed",
        "reason": "normal_hangup",
    }


async def test_on_call_end_terminal_event_carries_override_reason(
    async_session: AsyncSession,
) -> None:
    """A SIP-derived terminal status surfaces its real prior state + reason."""
    call_id = await _make_call_row(async_session)  # status='dialing' (never answered)

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="no_answer",
        end_reason_override="user_unavailable",
    )

    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].payload == {
        "from": "dialing",
        "to": "no_answer",
        "reason": "user_unavailable",
    }


async def test_on_call_end_bills_from_answered_at_when_present(
    async_session: AsyncSession,
) -> None:
    """Duration is billed from pickup (answered_at), not dial time (started_at).

    started_at is dial time; ring time is not billable. With answered_at set,
    only the conversation after pickup is metered.
    """
    call_id = await _make_call_row(async_session)
    now = datetime.now(timezone.utc)
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(
            status="in_progress",
            started_at=now - timedelta(seconds=120),  # dialed 120s ago
            answered_at=now - timedelta(seconds=30),  # picked up 30s ago
        )
    )
    await async_session.commit()

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    usage = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(usage) == 1
    # ~30s billed (from pickup), NOT ~120s (from dial). Wide-ish band for slack.
    assert 25_000 <= usage[0].units <= 35_000
    assert refreshed.status == "completed"


async def test_on_call_end_no_override_defaults_to_normal_hangup(
    async_session: AsyncSession,
) -> None:
    """No override → status='completed' AND end_reason='normal_hangup'.

    The call_end_reason ENUM + the migration's CHECK constraint require an
    end_reason on every terminal row, so the happy-path default must land.
    """
    call_id = await _make_call_row(async_session)

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "completed"
    assert refreshed.end_reason == "normal_hangup"


# --------------------------------------------------------------------------- #
# AI disclosure — proactive, real (not just prompt-hoped-for)
# --------------------------------------------------------------------------- #


async def test_speak_greeting_speaks_disclosure_before_first_message() -> None:
    """The disclosure and ``first_message`` are spoken as one turn, disclosure
    leading."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {"first_message": "Hi, calling about your order."})

    assert session.say_calls == [
        (f"{AI_DISCLOSURE_LINE} Hi, calling about your order.", True)
    ]


async def test_speak_greeting_skips_disclosure_when_opted_out() -> None:
    """``ai_disclosure: False`` skips the disclosure; ``first_message`` still
    plays. With neither, the LLM opening still fires — never dead air."""
    session = FakeAnnouncingSession()
    await speak_greeting(
        session, {"ai_disclosure": False, "first_message": "Hi, about your order."}
    )
    assert session.say_calls == [("Hi, about your order.", True)]
    assert session.generate_reply_calls == []

    session = FakeAnnouncingSession()
    await speak_greeting(session, {"ai_disclosure": False})
    assert session.say_calls == []
    assert session.generate_reply_calls == [opening_instructions(None)]


async def test_speak_greeting_speaks_disclosure_when_no_first_message() -> None:
    """No ``first_message`` → disclosure still spoken, then a generated
    opening turn instead of silence."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {})

    assert session.say_calls == [(AI_DISCLOSURE_LINE, True)]
    assert session.generate_reply_calls == [opening_instructions(None)]


async def test_speak_greeting_first_message_suppresses_generated_opening() -> None:
    """A caller ``first_message`` is the opening — no LLM turn on top of it."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {"first_message": "Hi, about your order."})

    assert session.generate_reply_calls == []


async def test_generated_opening_reacts_to_pickup_transcript() -> None:
    """The AMD pickup transcript flows into the opening instructions; silence
    (None/blank) gets the not-said-anything variant."""
    session = FakeAnnouncingSession()
    await speak_greeting(session, {}, pickup_transcript="Joe's Pizza, good evening?")
    assert len(session.generate_reply_calls) == 1
    assert "Joe's Pizza, good evening?" in session.generate_reply_calls[0]

    assert "not said anything" in opening_instructions("   ")
    assert "not said anything" in opening_instructions(None)


def test_speech_text_strips_tool_syntax() -> None:
    """Regression for call d8f4743f: fast-tier models leaked tool-call
    syntax as spoken turns during IVR navigation."""
    assert speech_text("```json\n{}\n") == ""
    assert speech_text('{"digits":"2"}') == ""
    assert speech_text("...") == ""
    assert speech_text("") == ""
    assert speech_text("No action needed.\n```json\n{}") == "No action needed."
    # The session's markdown filter strips fences before TTS, leaving bare
    # JSON on its own line — truncate from a newline-opening object/array.
    assert speech_text('Pressing two.\n{"digits":"2"}') == "Pressing two."
    assert speech_text("One moment, please.") == "One moment, please."


async def test_sanitize_tts_stream_filters_like_speech_text() -> None:
    """The streaming TTS filter matches speech_text: normal speech passes
    byte-identical, JSON-opening turns drop, fences truncate — even when
    the fence splits across chunk boundaries."""

    async def gen(*chunks: str):
        for c in chunks:
            yield c

    async def collect(*chunks: str) -> str:
        return "".join([c async for c in _sanitize_tts_stream(gen(*chunks))])

    assert await collect("One moment, ", "please.") == "One moment, please."
    assert await collect("```json", "\n{}") == ""
    assert await collect('{"digits"', ':"2"}') == ""
    assert await collect("Sure. `", "``json\n{}") == "Sure. "
    # Fence already stripped upstream by filter_markdown: truncate from the
    # newline-opening JSON remnant, split across chunks.
    assert await collect("Pressing two.", '\n{"digits":"2"}') == "Pressing two."
    # The sanctioned "..." nothing-to-say reply is dropped, not synthesized.
    assert await collect("...") == ""
    assert await collect("..", ". OK") == "... OK"


async def test_agent_turn_tool_syntax_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage assistant turns are neither spoken nor written to
    call_events; partially-speakable turns store only the spoken part."""
    recorded: list[tuple[str, dict]] = []

    async def _fake_write(call_id: UUID, kind: str, payload: dict) -> None:
        recorded.append((kind, payload))

    monkeypatch.setattr("hailhq.voicebot.agent.write_call_event", _fake_write)
    session = _FakeSession()
    tasks = attach_event_handlers(session, UUID("11111111-1111-1111-1111-111111111111"))
    on_item = session.handlers["conversation_item_added"]

    on_item(
        SimpleNamespace(
            item=SimpleNamespace(role="assistant", text_content="```json\n{}")
        )
    )
    assert not tasks, "pure tool-syntax turn must not spawn a write"

    on_item(
        SimpleNamespace(
            item=SimpleNamespace(
                role="assistant", text_content="No action needed.\n```json\n{}"
            )
        )
    )
    await asyncio.gather(*list(tasks))
    assert recorded == [
        ("agent_turn", {"role": "assistant", "text": "No action needed."})
    ]


async def test_tool_call_event_records_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tool_call events carry per-call args so consumers can show what a
    tool did (e.g. which DTMF digit was pressed)."""
    recorded: list[tuple[str, dict]] = []

    async def _fake_write(call_id: UUID, kind: str, payload: dict) -> None:
        recorded.append((kind, payload))

    monkeypatch.setattr("hailhq.voicebot.agent.write_call_event", _fake_write)
    session = _FakeSession()
    tasks = attach_event_handlers(session, UUID("11111111-1111-1111-1111-111111111111"))
    on_tools = session.handlers["function_tools_executed"]

    on_tools(
        SimpleNamespace(
            function_calls=[
                SimpleNamespace(name="send_dtmf", arguments='{"digits": "2"}')
            ]
        )
    )
    await asyncio.gather(*list(tasks))
    assert recorded == [
        (
            "tool_call",
            {
                "tools": ["send_dtmf"],
                "calls": [{"name": "send_dtmf", "args": {"digits": "2"}}],
            },
        )
    ]


async def test_speak_greeting_deferred_path_skips_generated_opening() -> None:
    """``generate_opening=False`` (the deferred IVR path) fires no opening
    turn: the greeting runs because a person just spoke, and the session's
    normal turn loop answers them. Disclosure still plays."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {}, generate_opening=False)

    assert session.say_calls == [(AI_DISCLOSURE_LINE, True)]
    assert session.generate_reply_calls == []


async def test_speak_greeting_first_message_cannot_precede_disclosure() -> None:
    """Even a caller-supplied ``first_message`` can't be spoken first —
    the disclosure is not sourced from (and cannot be overridden by)
    caller-controlled metadata like ``system_prompt`` or ``first_message``.
    """
    session = FakeAnnouncingSession()

    await speak_greeting(
        session, {"first_message": AI_DISCLOSURE_LINE + " (impersonated)"}
    )

    assert len(session.say_calls) == 1
    text, allow_interruptions = session.say_calls[0]
    assert text.startswith(AI_DISCLOSURE_LINE)
    assert allow_interruptions is True


async def test_speak_greeting_names_the_org_when_dispatch_metadata_has_it() -> None:
    """org_name (server-resolved, not caller-supplied) is spoken in the
    disclosure — 47 CFR 64.1200(b)(1) wants the initiating business named."""
    session = FakeAnnouncingSession()

    await speak_greeting(session, {"org_name": "Acme Corp"})

    assert session.say_calls == [
        ("Hi, this is an AI assistant calling on behalf of Acme Corp.", True)
    ]


async def test_speak_greeting_blank_or_missing_org_name_falls_back_to_generic() -> None:
    for meta in ({}, {"org_name": None}, {"org_name": ""}, {"org_name": "   "}):
        session = FakeAnnouncingSession()
        await speak_greeting(session, meta)
        assert session.say_calls == [(AI_DISCLOSURE_LINE, True)]


# --------------------------------------------------------------------------- #
# Soft-cap behavior — voice call duration limit
# --------------------------------------------------------------------------- #


async def test_soft_cap_speaks_announcement_then_shuts_down() -> None:
    """After the delay, agent says the cap line (uninterruptible), waits
    for playout, then calls ctx.shutdown(SOFT_CAP_END_REASON)."""
    ctx = FakeJobContext()
    session = FakeAnnouncingSession()
    call_id = UUID("11111111-2222-3333-4444-555555555555")

    await soft_cap_announce_and_hangup(ctx, session, call_id, delay_seconds=0)  # type: ignore[arg-type]

    assert len(session.say_calls) == 1
    text, allow_interruptions = session.say_calls[0]
    assert text == SOFT_CAP_ANNOUNCEMENT
    # Uninterruptible — the caller must actually hear the goodbye before
    # the line drops.
    assert allow_interruptions is False
    assert session.last_handle is not None
    assert session.last_handle.played_out is True
    assert ctx.shutdown_calls == [SOFT_CAP_END_REASON]


async def test_soft_cap_cancelled_before_firing_does_not_speak() -> None:
    """If the call ends naturally before the cap, the task is cancelled
    and no announcement is spoken."""
    ctx = FakeJobContext()
    session = FakeAnnouncingSession()
    call_id = UUID("11111111-2222-3333-4444-555555555556")

    task = asyncio.create_task(
        soft_cap_announce_and_hangup(ctx, session, call_id, delay_seconds=60)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)  # let the task reach its sleep
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert session.say_calls == []
    assert ctx.shutdown_calls == []


# --------------------------------------------------------------------------- #
# Agent-initiated hangup (end_call tool) — must finalize as a normal hangup,
# not a worker_shutdown/failed (Task 9)
# --------------------------------------------------------------------------- #


async def test_agent_hangup_marks_normal_hangup() -> None:
    """The end_call tool's hangup handle stamps end_reason BEFORE shutdown.

    Without the stamp, `_on_session_close` would map the resulting bare
    `job_shutdown` to `worker_shutdown`/`failed`, mis-recording a deliberate,
    successful agent-initiated hangup. Status must stay None so
    `on_call_end` falls back to its `"completed"` default.
    """
    captured: dict[str, str | None] = {"status": None, "end_reason": None}
    ctx = FakeJobContext()
    hangup = make_agent_hangup(ctx, captured)  # type: ignore[arg-type]

    await hangup()

    assert captured["end_reason"] == CallEndReason.NORMAL_HANGUP.value
    assert captured["status"] is None
    assert ctx.shutdown_calls == ["agent_end_call"]
    # ctx.shutdown() only ends the agent job — per LiveKit docs the SIP
    # participant keeps hearing silence until the ROOM is deleted. The
    # hangup handle must delete the room to actually end the phone call.
    assert ctx.delete_room_calls == 1


async def test_agent_hangup_still_shuts_down_when_delete_room_fails() -> None:
    """A delete_room failure (API blip) must not strand the job: shutdown
    still runs so on_call_end finalizes the row; the room then dies via
    LiveKit's empty-timeout instead."""
    captured: dict[str, str | None] = {"status": None, "end_reason": None}
    ctx = FakeJobContext()
    ctx.delete_room_error = RuntimeError("twirp unavailable")
    hangup = make_agent_hangup(ctx, captured)  # type: ignore[arg-type]

    await hangup()

    assert captured["end_reason"] == CallEndReason.NORMAL_HANGUP.value
    assert ctx.delete_room_calls == 1
    assert ctx.shutdown_calls == ["agent_end_call"]


async def test_agent_send_dtmf_publishes_rfc4733_code() -> None:
    """The DTMF handle owns only transport; core owns the digit vocabulary."""
    ctx = FakeJobContext()
    send_dtmf = make_agent_send_dtmf(ctx)  # type: ignore[arg-type]

    await send_dtmf("#")
    await send_dtmf("7")

    assert ctx.room.local_participant.dtmf == [(11, "#"), (7, "7")]


async def test_on_call_end_bills_a_machine_answered_call(
    async_session: AsyncSession,
) -> None:
    """A voicemail box that picked up burned real minutes — bill them.

    Billing keys off `answered_at` (the SIP leg went active), not the final
    status, so an AMD hangup recorded as `no_answer` is still billed.
    """
    call_id = await _make_call_row(async_session)
    answered = datetime.now(timezone.utc) - timedelta(seconds=12)
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=answered, answered_at=answered)
    )
    await async_session.commit()

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="no_answer",
        end_reason_override=CallEndReason.VOICEMAIL_REACHED.value,
    )

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)
    assert refreshed.status == "no_answer"
    assert refreshed.end_reason == CallEndReason.VOICEMAIL_REACHED.value

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert 10_000 <= rows[0].units <= 20_000


async def test_on_call_end_bills_a_completed_call_with_no_answered_at(
    async_session: AsyncSession,
) -> None:
    """A completed call whose answer signal never landed must still bill.

    `mark_call_answered` can miss (DB blip, dropped `sip.callStatus` event),
    and legacy rows predate `answered_at` entirely. Billing falls back to
    `started_at` for those, so `status='completed'` remains a sufficient
    condition alongside `answered_at`.
    """
    call_id = await _make_call_row(async_session)
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=datetime.now(timezone.utc) - timedelta(seconds=45))
    )
    await async_session.commit()

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert 40_000 <= rows[0].units <= 55_000


async def test_on_call_end_does_not_bill_a_call_that_never_answered(
    async_session: AsyncSession,
) -> None:
    """A genuine no-answer rang but never picked up — nothing to bill."""
    call_id = await _make_call_row(async_session)
    # started_at (dial time) is set, answered_at is not: pure ring time.
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=datetime.now(timezone.utc) - timedelta(seconds=30))
    )
    await async_session.commit()

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="no_answer",
        end_reason_override=CallEndReason.USER_UNAVAILABLE.value,
    )

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def test_on_call_end_bills_a_call_that_failed_mid_conversation(
    async_session: AsyncSession,
) -> None:
    """Deliberate widening: answered-then-failed minutes were consumed too."""
    call_id = await _make_call_row(async_session)
    answered = datetime.now(timezone.utc) - timedelta(seconds=20)
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(status="in_progress", started_at=answered, answered_at=answered)
    )
    await async_session.commit()

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override="failed",
        end_reason_override=CallEndReason.AGENT_ERROR.value,
    )

    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == str(call_id))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_build_tools_safely_degrades_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool-layer startup failure must never kill the call.

    `build_tools_safely` is the entrypoint's guard around `build_agent_tools`:
    any exception (e.g. a DB rollback on a dead connection) degrades to
    `([], None)` — no tools beats a dead call.
    """
    from hailhq.voicebot import agent as agent_mod

    async def _boom(
        _metadata: dict, *, call_id: UUID, hangup: object, send_dtmf: object
    ) -> tuple:
        raise RuntimeError("dead connection during availability checks")

    monkeypatch.setattr(agent_mod, "build_agent_tools", _boom)

    async def _hangup() -> None:
        return None

    async def _send_dtmf(_digit: str) -> None:
        return None

    call_id = UUID("11111111-2222-3333-4444-555555555557")
    # Must not raise.
    tools, api = await build_tools_safely({}, call_id, _hangup, _send_dtmf)

    assert tools == []
    assert api is None


async def test_build_tools_safely_passes_through_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The happy path is a pure passthrough of `build_agent_tools`' result."""
    from hailhq.voicebot import agent as agent_mod

    sentinel_tools = [object()]
    sentinel_api = object()

    async def _ok(
        _metadata: dict, *, call_id: UUID, hangup: object, send_dtmf: object
    ) -> tuple:
        return sentinel_tools, sentinel_api

    monkeypatch.setattr(agent_mod, "build_agent_tools", _ok)

    async def _hangup() -> None:
        return None

    async def _send_dtmf(_digit: str) -> None:
        return None

    call_id = UUID("11111111-2222-3333-4444-555555555558")
    tools, api = await build_tools_safely({}, call_id, _hangup, _send_dtmf)

    assert tools is sentinel_tools
    assert api is sentinel_api
