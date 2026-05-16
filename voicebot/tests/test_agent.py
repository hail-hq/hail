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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
from livekit import rtc
from livekit.agents import Agent, AgentSession
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Call, CallEvent, PhoneNumber, UsageEvent
from hailhq.core.pool import CALL_META_FROM_POOL
from hailhq.voicebot.agent import (
    SOFT_CAP_ANNOUNCEMENT,
    SOFT_CAP_END_REASON,
    attach_event_handlers,
    disconnect_reason_to_status,
    on_call_end,
    parse_metadata,
    soft_cap_announce_and_hangup,
)

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
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
        status="dialing",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call.id


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
    # Backdate started_at so the (now - started_at) delta is roughly 60s.
    await async_session.execute(
        update(Call)
        .where(Call.id == call_id)
        .values(started_at=datetime.now(timezone.utc) - timedelta(seconds=60))
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
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
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
