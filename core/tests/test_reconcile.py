"""Stale-call reconciler — backstop that force-closes stuck non-terminal calls.

The hot path closes a call via the voicebot's ``on_call_end`` shutdown
callback. When that never runs (worker crash, dropped LiveKit teardown), the
call pins at a non-terminal status forever. :func:`sweep_stale_calls` is the
deterministic backstop, mirroring :func:`sweep_pool_reservations`'s bounds
(``COALESCE(started_at, requested_at) + max_duration_seconds + grace``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from hailhq.core.models import Call, CallEvent, PhoneNumber
from hailhq.core.reconcile import sweep_stale_calls


async def _make_call(
    session,
    *,
    status: str = "dialing",
    started_at: datetime | None = None,
    requested_at: datetime | None = None,
    max_duration_seconds: int | None = 300,
    end_reason: str | None = None,
) -> Call:
    pn = PhoneNumber(
        organization_id=uuid.uuid4(),
        e164=f"+1415555{uuid.uuid4().int % 10000:04d}",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    call = Call(
        organization_id=pn.organization_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155551234",
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
        status=status,
        max_duration_seconds=max_duration_seconds,
        end_reason=end_reason,
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)

    # started_at / requested_at are set post-insert so we can backdate them.
    values: dict = {}
    if started_at is not None:
        values["started_at"] = started_at
    if requested_at is not None:
        values["requested_at"] = requested_at
    if values:
        await session.execute(update(Call).where(Call.id == call.id).values(**values))
        await session.commit()
        await session.refresh(call)
    return call


async def _state_change_events(session, call_id: uuid.UUID) -> list[CallEvent]:
    return list(
        (
            await session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call_id, CallEvent.kind == "state_change"
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_sweep_force_closes_stale_nonterminal_call(async_session):
    """A dialing call past max_duration + grace is failed with sweeper_timeout."""
    now = datetime.now(timezone.utc)
    call = await _make_call(
        async_session,
        status="dialing",
        started_at=now - timedelta(seconds=600),  # 10 min ago
        max_duration_seconds=300,  # +120 grace = 420s bound, well exceeded
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()

    assert call.id in swept
    await async_session.refresh(call)
    assert call.status == "failed"
    assert call.end_reason == "sweeper_timeout"
    assert call.ended_at is not None

    events = await _state_change_events(async_session, call.id)
    assert len(events) == 1
    assert events[0].payload == {
        "from": "dialing",
        "to": "failed",
        "reason": "sweeper_timeout",
    }


@pytest.mark.asyncio
async def test_sweep_uses_requested_at_when_never_started(async_session):
    """A queued call that never dialed (started_at NULL) bounds off requested_at."""
    now = datetime.now(timezone.utc)
    call = await _make_call(
        async_session,
        status="queued",
        started_at=None,
        requested_at=now - timedelta(seconds=600),
        max_duration_seconds=300,
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()

    assert call.id in swept
    await async_session.refresh(call)
    assert call.status == "failed"
    assert call.end_reason == "sweeper_timeout"
    events = await _state_change_events(async_session, call.id)
    assert events[0].payload["from"] == "queued"


@pytest.mark.asyncio
async def test_sweep_leaves_live_call_alone(async_session):
    """A fresh in_progress call within its bound is untouched."""
    now = datetime.now(timezone.utc)
    call = await _make_call(
        async_session,
        status="in_progress",
        started_at=now,
        max_duration_seconds=300,
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()

    assert call.id not in swept
    await async_session.refresh(call)
    assert call.status == "in_progress"
    assert await _state_change_events(async_session, call.id) == []


@pytest.mark.asyncio
async def test_sweep_ignores_already_terminal_call(async_session):
    """A completed call is never re-closed (no double-write, no extra event)."""
    now = datetime.now(timezone.utc)
    call = await _make_call(
        async_session,
        status="completed",
        started_at=now - timedelta(seconds=600),
        max_duration_seconds=300,
        end_reason="normal_hangup",
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()

    assert call.id not in swept
    await async_session.refresh(call)
    assert call.status == "completed"
    assert call.end_reason == "normal_hangup"
    assert await _state_change_events(async_session, call.id) == []


@pytest.mark.asyncio
async def test_sweep_respects_max_duration_snapshot(async_session):
    """A large snapshot max_duration keeps a long call alive past short bounds."""
    now = datetime.now(timezone.utc)
    call = await _make_call(
        async_session,
        status="in_progress",
        started_at=now - timedelta(seconds=400),
        max_duration_seconds=1800,  # 30 min — still well within bound
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()
    assert call.id not in swept


@pytest.mark.asyncio
async def test_sweep_noop_when_nothing_stuck(async_session):
    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    assert swept == []


@pytest.mark.asyncio
async def test_sweep_handles_multiple_calls_in_one_pass(async_session):
    now = datetime.now(timezone.utc)
    a = await _make_call(
        async_session,
        status="dialing",
        started_at=now - timedelta(seconds=600),
        max_duration_seconds=300,
    )
    b = await _make_call(
        async_session,
        status="ringing",
        started_at=now - timedelta(seconds=600),
        max_duration_seconds=300,
    )

    swept = await sweep_stale_calls(async_session, grace_seconds=120)
    await async_session.commit()
    assert set(swept) == {a.id, b.id}
