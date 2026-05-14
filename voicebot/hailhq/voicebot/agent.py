"""LiveKit Agents entrypoint + lifecycle for the Hail voicebot.

CallEvent dedupe — known limitation: if the LiveKit dispatcher redispatches
the worker for the same call, duplicate ``call_events`` rows are accepted.
v1 does not constrain this; tracked as a follow-up.

Verified 2026-04-28 against:

* ``livekit-agents/livekit/agents/job.py`` — ``JobContext.connect``,
  ``ctx.job.metadata`` (``str``), ``ctx.add_shutdown_callback``,
  ``ctx.proc.userdata``.
* ``livekit-agents/livekit/agents/voice/agent_session.py`` — ``start()``
  and ``on()`` (inherits ``rtc.EventEmitter``).
* ``livekit-agents/livekit/agents/voice/events.py`` — event type strings
  (``conversation_item_added``, ``function_tools_executed``, ``error``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from livekit.agents import Agent, JobContext, JobProcess
from livekit.agents.voice import AgentSession
from livekit.plugins import silero
from sqlalchemy import select, update

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.internal_webhook import notify_usage_event_recorded
from hailhq.core.pool import release_pool_reservation
from hailhq.core.models import Call, CallEvent, UsageEvent
from hailhq.voicebot.pipeline import build_session
from hailhq.voicebot.recording import upload_recording

# Soft-cap announcement spoken when a call hits HAIL_VOICE_MAX_DURATION_SECONDS.
# Phrased like an honest operator note rather than a robotic cutoff so the
# caller has a moment to say their goodbyes.
SOFT_CAP_ANNOUNCEMENT = (
    "Sorry, we've reached the time limit for this call. "
    "Thanks for talking — I'll let you go now. Goodbye."
)

# Shutdown reason passed to ctx.shutdown() when the soft cap fires.
SOFT_CAP_END_REASON = "soft_cap_reached"

logger = logging.getLogger("hailhq.voicebot")


def prewarm(proc: JobProcess) -> None:
    """Load Silero VAD once per worker process.

    ``WorkerOptions.prewarm_fnc`` runs in the parent of each forked job
    process; the loaded VAD is cached in ``proc.userdata`` and reused by
    every session this process serves.
    """
    proc.userdata["vad"] = silero.VAD.load()


def parse_metadata(raw: str | None) -> dict[str, Any]:
    """Parse the JSON metadata the API service attached to the dispatch.

    Required: ``call_id`` (returned as a parsed :class:`UUID`). Optional:
    ``voice_config``, ``system_prompt``, ``llm`` (None → mode A fallback
    chain), ``first_message``.
    """
    payload = json.loads(raw) if raw else {}
    if "call_id" not in payload:
        raise ValueError(
            "dispatch metadata missing required field 'call_id'; check the API "
            "service's CreateAgentDispatchRequest payload"
        )
    payload["call_id"] = UUID(str(payload["call_id"]))
    return payload


async def write_call_event(call_id: UUID, kind: str, payload: dict[str, Any]) -> None:
    """Append one ``call_events`` row in a fresh transaction.

    Each event lands in its own ``session_scope`` so a DB blip on event N
    doesn't disrupt event N+1. Errors are logged and swallowed so the agent
    loop keeps running.
    """
    try:
        async with session_scope() as session:
            session.add(CallEvent(call_id=call_id, kind=kind, payload=payload))
            await session.commit()
    except Exception:  # pragma: no cover
        logger.warning(
            "call_events insert failed for call_id=%s kind=%s",
            call_id,
            kind,
            exc_info=True,
        )


async def soft_cap_announce_and_hangup(
    ctx: JobContext,
    session: AgentSession,
    call_id: UUID,
    delay_seconds: int,
) -> None:
    """Wait ``delay_seconds`` then politely end the call.

    Soft cap (not a hard cutoff): the agent speaks the announcement,
    waits for playback to finish so the caller actually hears it, then
    requests ``ctx.shutdown()``. The existing shutdown callback runs
    ``on_call_end`` → writes the ``usage_events`` row → fires the rater
    webhook. Same lifecycle as a natural call end.

    Cancellable: if the call ends naturally before the cap, the
    entrypoint cancels this task and the announcement never plays.
    """
    try:
        await asyncio.sleep(delay_seconds)
    except asyncio.CancelledError:
        return

    logger.info(
        "call_id=%s reached %ds soft-cap, announcing and ending",
        call_id,
        delay_seconds,
    )
    try:
        handle = session.say(SOFT_CAP_ANNOUNCEMENT, allow_interruptions=False)
        await handle.wait_for_playout()
    except Exception:  # pragma: no cover — best-effort; we still hang up
        logger.exception(
            "call_id=%s soft-cap announcement failed; proceeding to hangup",
            call_id,
        )
    ctx.shutdown(reason=SOFT_CAP_END_REASON)


async def on_call_end(call_id: UUID, room_name: str) -> None:
    """Finalize the ``Call`` row when the session ends.

    Called from a shutdown callback registered against ``ctx``. Uploads the
    recording (no-op in v1 — see :mod:`hailhq.voicebot.recording`), marks
    the call ``completed`` with ``ended_at`` set to ``now()``, and records
    one raw ``usage_events`` row with the call duration in milliseconds.

    No money math happens here. The website's private rater converts the
    raw units into a dollar debit against ``account_credits`` using its
    private cents-per-unit rates. Self-host operators (no website running)
    see ``usage_events`` accumulate as a generic analytics primitive they
    can query directly.
    """
    recording_key = await upload_recording(call_id, room_name)
    now = datetime.now(timezone.utc)

    async with session_scope() as session:
        # Pull started_at + organization_id in the same txn we'll write back.
        # Falls back to recording_duration_ms when started_at is missing
        # (call failed before SIP answered → no billable duration).
        stmt = select(
            Call.started_at,
            Call.organization_id,
            Call.recording_duration_ms,
        ).where(Call.id == call_id)
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            logger.warning("on_call_end: call_id=%s not found", call_id)
            return
        started_at, organization_id, recording_duration_ms = row

        if started_at is not None:
            duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
        elif recording_duration_ms is not None:
            duration_ms = int(recording_duration_ms)
        else:
            duration_ms = 0

        await session.execute(
            update(Call)
            .where(Call.id == call_id)
            .values(
                status="completed",
                ended_at=now,
                recording_s3_key=recording_key,
            )
        )
        # Same transaction as the status update — a rollback (e.g. failed
        # usage_events insert) must also unwind the release so the sweeper
        # backstop can retry. No-op for non-pool calls.
        await release_pool_reservation(session, call_id=call_id)
        usage_event_id: str | None = None
        if duration_ms > 0:
            usage = UsageEvent(
                organization_id=organization_id,
                channel="voice",
                units=duration_ms,
                ref=str(call_id),
            )
            session.add(usage)
            await session.flush()
            usage_event_id = str(usage.id)
        await session.commit()

    if usage_event_id is not None:
        notify_usage_event_recorded(usage_event_id)


def attach_event_handlers(
    session: AgentSession, call_id: UUID
) -> set[asyncio.Task[None]]:
    """Wire AgentSession events to ``call_events`` row writes.

    Returns the set of pending row-write tasks so the caller can ``gather``
    them before final cleanup; without this, fire-and-forget writes from the
    last few events can be cut off by shutdown. Handlers schedule async work
    via ``asyncio.ensure_future`` because LiveKit's EventEmitter dispatches
    sync callbacks.
    """
    tasks: set[asyncio.Task[None]] = set()

    def _spawn(kind: str, payload: dict[str, Any]) -> None:
        task = asyncio.ensure_future(write_call_event(call_id, kind, payload))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        item = ev.item
        role = getattr(item, "role", None)
        if role == "user":
            kind = "user_turn"
        elif role == "assistant":
            kind = "agent_turn"
        else:
            return
        _spawn(kind, {"role": role, "text": getattr(item, "text_content", "") or ""})

    @session.on("function_tools_executed")
    def _on_tools(ev: Any) -> None:
        _spawn("tool_call", {"tools": [c.name for c in ev.function_calls]})

    @session.on("error")
    def _on_error(ev: Any) -> None:
        _spawn("error", {"error": str(ev.error)[:500]})

    return tasks


async def entrypoint(ctx: JobContext) -> None:
    """The function ``WorkerOptions.entrypoint_fnc`` points at."""
    metadata = parse_metadata(ctx.job.metadata)
    call_id: UUID = metadata["call_id"]

    await ctx.connect()

    vad = ctx.proc.userdata["vad"]
    session = build_session(metadata.get("llm"), vad)
    event_tasks = attach_event_handlers(session, call_id)

    agent = Agent(instructions=metadata.get("system_prompt") or "")
    await session.start(agent=agent, room=ctx.room)

    if metadata.get("first_message"):
        await session.say(metadata["first_message"], allow_interruptions=True)

    room_name = ctx.room.name

    soft_cap_seconds = settings.hail_voice_max_duration_seconds
    soft_cap_task: asyncio.Task[None] | None = None
    if soft_cap_seconds > 0:
        soft_cap_task = asyncio.create_task(
            soft_cap_announce_and_hangup(ctx, session, call_id, soft_cap_seconds)
        )

    async def _shutdown() -> None:
        if soft_cap_task is not None and not soft_cap_task.done():
            soft_cap_task.cancel()
            await asyncio.gather(soft_cap_task, return_exceptions=True)
        if event_tasks:
            await asyncio.gather(*list(event_tasks), return_exceptions=True)
        await on_call_end(call_id, room_name)

    ctx.add_shutdown_callback(_shutdown)


__all__ = [
    "SOFT_CAP_ANNOUNCEMENT",
    "SOFT_CAP_END_REASON",
    "attach_event_handlers",
    "entrypoint",
    "on_call_end",
    "parse_metadata",
    "prewarm",
    "soft_cap_announce_and_hangup",
    "write_call_event",
]
