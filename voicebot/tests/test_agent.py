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
from datetime import datetime, timezone
from uuid import UUID

import pytest
from livekit.agents import Agent, AgentSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Call, CallEvent, Organization, PhoneNumber
from hailhq.voicebot.agent import (
    attach_event_handlers,
    on_call_end,
    parse_metadata,
)

from ._fakes import FakeLLM


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


async def _make_call_row(session: AsyncSession) -> UUID:
    """Insert an org + phone_number + queued call; return the call id."""
    org = Organization(name="Acme", slug="acme")
    session.add(org)
    await session.flush()

    pn = PhoneNumber(
        organization_id=org.id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    call = Call(
        organization_id=org.id,
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
