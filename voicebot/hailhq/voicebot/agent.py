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
from typing import Any, Callable
from uuid import UUID

from livekit import rtc
from livekit.agents import Agent, JobContext, JobProcess
from livekit.agents.voice import AgentSession
from livekit.plugins import silero
from sqlalchemy import select, update

from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.internal_webhook import notify_usage_event_recorded
from hailhq.core.pool import release_pool_reservation
from hailhq.core.models import Call, CallEvent, UsageEvent
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.voicebot.pipeline import build_session
from hailhq.voicebot.recording import upload_recording

# Fixed voice-context framing prepended to every agent's instructions. The
# bundled fallback LLM (mode A) and a caller-supplied prompt both default to a
# generic *chat* assistant self-concept; on a live call that produced an agent
# insisting it was "text-based" and couldn't hear audio. This preamble always
# leads the instructions (see `build_instructions`) so the caller's prompt
# *adds to* the voice framing rather than replacing it. The no-emoji line is
# the real fix for emoji reaching TTS: the LLM hands its raw text to the TTS
# engine inside the pipeline, so we have to stop emission at the source — the
# `conversation_item_added` transcript we store never touches TTS.
VOICE_PREAMBLE = (
    "You are on a live telephone call. You hear the other party through "
    "speech-to-text and you reply through text-to-speech — you are a voice "
    "assistant, not a text-based chat assistant. Never say you are "
    '"text-based" or that you cannot hear audio; you can hear the caller. '
    "You are an AI voice assistant placing this call on behalf of the person "
    "who set it up. If asked, say that plainly, and never claim to be human. "
    "Keep replies short — one or two spoken sentences — then pause to let the "
    "other party respond. Speak in plain words only: no emoji, markdown, or "
    "other symbols that cannot be read aloud."
)


def build_instructions(system_prompt: str | None) -> str:
    """Assemble the agent's instructions: voice preamble first, caller prompt after.

    The :data:`VOICE_PREAMBLE` is non-overridable framing — it always leads.
    A caller-supplied ``system_prompt`` is appended after it (separated by a
    blank line) so callers customize the task without losing the voice-call
    self-concept. When the caller supplies nothing, the preamble alone is the
    instruction set. Mode-agnostic: applies identically to the mode A fallback
    chain and a mode B BYO endpoint, since instructions are wired once here
    regardless of which LLM the session uses.
    """
    caller = (system_prompt or "").strip()
    if not caller:
        return VOICE_PREAMBLE
    return f"{VOICE_PREAMBLE}\n\n{caller}"


# Soft-cap announcement spoken when a call hits HAIL_VOICE_MAX_DURATION_SECONDS.
# Phrased like an honest operator note rather than a robotic cutoff so the
# caller has a moment to say their goodbyes.
SOFT_CAP_ANNOUNCEMENT = (
    "Sorry, we've reached the time limit for this call. "
    "Thanks for talking — I'll let you go now. Goodbye."
)

# Shutdown reason passed to ctx.shutdown() when the soft cap fires. Value
# matches the call_end_reason ENUM so it can land directly in the calls row.
SOFT_CAP_END_REASON: str = CallEndReason.SOFT_CAP_REACHED.value

# Reasons the LiveKit Agents SDK closes the session on automatically. Mirror
# of livekit-agents 1.5.x `room_io.types.DEFAULT_CLOSE_ON_DISCONNECT_REASONS`.
# Encoded locally because that module is not part of the public surface.
# For any other reason that we map to a Call.status, we have to call
# ctx.shutdown() ourselves or the session sits idle until the worker timeout.
_SDK_AUTO_CLOSE_REASONS: frozenset[int] = frozenset(
    {
        rtc.DisconnectReason.CLIENT_INITIATED,
        rtc.DisconnectReason.ROOM_DELETED,
        rtc.DisconnectReason.USER_REJECTED,
    }
)


_DISCONNECT_REASON_MAP: dict[int, tuple[str, CallEndReason]] = {
    rtc.DisconnectReason.USER_UNAVAILABLE: (
        "no_answer",
        CallEndReason.USER_UNAVAILABLE,
    ),
    rtc.DisconnectReason.USER_REJECTED: ("busy", CallEndReason.USER_REJECTED),
    rtc.DisconnectReason.SIP_TRUNK_FAILURE: ("failed", CallEndReason.SIP_TRUNK_FAILURE),
    rtc.DisconnectReason.CONNECTION_TIMEOUT: (
        "failed",
        CallEndReason.CONNECTION_TIMEOUT,
    ),
    rtc.DisconnectReason.MEDIA_FAILURE: ("failed", CallEndReason.MEDIA_FAILURE),
}


# LiveKit SIP participant attribute carrying the live call state. For an
# outbound call it walks `dialing` → `active` when the callee picks up; `active`
# is our answer signal. Verified 2026-06-05 against
# docs.livekit.io/reference/telephony/sip-participant (SIP attributes table).
# NB: `participant_connected` fires when the trunk *accepts the INVITE* (dial
# time), not on pickup, so we key off this attribute reaching `active` rather
# than the connect event.
SIP_CALL_STATUS_ATTRIBUTE = "sip.callStatus"
SIP_CALL_STATUS_ACTIVE = "active"


def is_sip_answer_signal(participant: rtc.Participant) -> bool:
    """True when ``participant`` is the SIP leg and its call just went active.

    The callee answering is the only thing that should flip a call to
    ``in_progress``; the agent's own participant and any non-SIP participant
    are ignored.
    """
    return (
        participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
        and participant.attributes.get(SIP_CALL_STATUS_ATTRIBUTE)
        == SIP_CALL_STATUS_ACTIVE
    )


def disconnect_reason_to_status(reason: int | None) -> tuple[str | None, str | None]:
    """Map a LiveKit ``rtc.DisconnectReason`` to ``(call_status, end_reason)``.

    Returns ``(None, None)`` when the disconnect should NOT override the
    default ``"completed"`` Call status — i.e., the callee answered and
    hung up normally (``CLIENT_INITIATED``) or the reason is something we
    do not specifically map. The caller's default applies in that case.

    The ``end_reason`` strings are members of the ``call_end_reason``
    Postgres ENUM (see :class:`hailhq.core.call_end_reasons.CallEndReason`).
    """
    if reason is None:
        return (None, None)
    mapped = _DISCONNECT_REASON_MAP.get(reason)
    if mapped is None:
        return (None, None)
    status, end_reason = mapped
    return (status, end_reason.value)


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
    on_fire: Callable[[], None] | None = None,
) -> None:
    """Wait ``delay_seconds`` then politely end the call.

    Soft cap (not a hard cutoff): the agent speaks the announcement,
    waits for playback to finish so the caller actually hears it, then
    requests ``ctx.shutdown()``. The existing shutdown callback runs
    ``on_call_end`` → writes the ``usage_events`` row → fires the rater
    webhook. Same lifecycle as a natural call end.

    Cancellable: if the call ends naturally before the cap, the
    entrypoint cancels this task and the announcement never plays.

    ``on_fire`` is called synchronously *just before* ``ctx.shutdown`` if
    the cap actually fires (i.e. not cancelled). The entrypoint uses this
    hook to stamp ``end_reason='soft_cap_reached'`` in its captured-state
    dict so ``on_call_end`` writes the right value.
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
    if on_fire is not None:
        on_fire()
    ctx.shutdown(reason=SOFT_CAP_END_REASON)


async def mark_call_answered(call_id: UUID) -> bool:
    """Transition a dialing/ringing call to ``in_progress`` and stamp ``answered_at``.

    Called when the SIP participant's ``sip.callStatus`` reaches ``active``
    (see :func:`is_sip_answer_signal`). The ``answered_at`` stamp is the
    pickup timestamp — distinct from ``started_at`` (dial time) — and is what
    :func:`on_call_end` bills from.

    Returns ``True`` iff this call actually transitioned a row. Idempotent and
    race-safe by construction: the guard matches only a not-yet-answered,
    non-terminal row, so a duplicate attribute event, the API's slightly-later
    ``dialing`` write, or an already-terminal call all fall through to a no-op
    (and return ``False``). The ``state_change`` event is written only on a
    real transition, so observers see exactly one ``dialing → in_progress``.
    The boolean lets the caller latch its dedupe flag on success only, so a
    signal that arrives a hair before the ``dialing`` write can be retried.
    """
    now = datetime.now(timezone.utc)
    async with session_scope() as session:
        result = await session.execute(
            update(Call)
            .where(
                Call.id == call_id,
                Call.status.in_(("dialing", "ringing")),
                Call.answered_at.is_(None),
            )
            .values(status="in_progress", answered_at=now)
        )
        transitioned = (result.rowcount or 0) > 0
        if transitioned:
            # `from` is always `dialing` in practice — we never write `ringing`
            # for outbound (LiveKit only exposes `ringing` inbound) — but the
            # guard tolerates it so a future inbound path stays correct.
            session.add(
                CallEvent(
                    call_id=call_id,
                    kind="state_change",
                    payload={"from": "dialing", "to": "in_progress"},
                )
            )
        await session.commit()
    return transitioned


async def on_call_end(
    call_id: UUID,
    room_name: str,
    status_override: str | None = None,
    end_reason_override: str | None = None,
) -> None:
    """Finalize the ``Call`` row when the session ends.

    Called from a shutdown callback registered against ``ctx``. Uploads the
    recording (no-op in v1 — see :mod:`hailhq.voicebot.recording`), marks
    the call with the final status (``"completed"`` by default), sets
    ``ended_at = now()``, and records one raw ``usage_events`` row with the
    call duration in milliseconds.

    ``status_override`` lets the entrypoint pass a SIP-derived terminal
    status (e.g. ``"no_answer"`` from ``DisconnectReason.USER_UNAVAILABLE``)
    when the call ended without a real conversation; in those cases
    ``end_reason_override`` carries the lowercased DisconnectReason name
    for operational triage. ``None`` for both keeps the existing happy-path
    behavior (``status="completed"``, ``end_reason`` unchanged).

    Idempotent against a row that is already terminal: the status UPDATE is
    guarded ``status NOT IN terminal``, so if the reconciler sweeper (or a
    prior shutdown) already closed this call, this call no-ops — it does not
    overwrite the recorded outcome, emit a contradictory ``state_change``, or
    write a duplicate ``usage_events`` row (``usage_events.ref`` is not unique).
    The pool release still runs unconditionally (it is idempotent) so the
    number is never leaked regardless of which writer recorded the terminal.

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
            Call.status,
            Call.started_at,
            Call.answered_at,
            Call.organization_id,
            Call.recording_duration_ms,
        ).where(Call.id == call_id)
        row = (await session.execute(stmt)).one_or_none()
        if row is None:
            logger.warning("on_call_end: call_id=%s not found", call_id)
            return
        (
            prior_status,
            started_at,
            answered_at,
            organization_id,
            recording_duration_ms,
        ) = row

        # Bill from pickup (answered_at) when we have it — ring time before the
        # callee answered isn't conversation and isn't billable. Fall back to
        # started_at (dial time) for legacy rows or calls that closed without a
        # recorded answer.
        billed_from = answered_at or started_at
        if billed_from is not None:
            duration_ms = max(0, int((now - billed_from).total_seconds() * 1000))
        elif recording_duration_ms is not None:
            duration_ms = int(recording_duration_ms)
        else:
            duration_ms = 0

        final_status = status_override or "completed"
        # Always populate end_reason — the call_end_reason ENUM + CHECK
        # constraint requires it for terminal rows. Defaults:
        #   - status='completed' with no override → 'normal_hangup'
        #   - any other override-less terminal write → 'unknown' (defensive;
        #     in practice the entrypoint passes both overrides together).
        if end_reason_override is not None:
            final_end_reason = end_reason_override
        elif final_status == "completed":
            final_end_reason = CallEndReason.NORMAL_HANGUP.value
        else:
            final_end_reason = CallEndReason.UNKNOWN.value
        # Guard on a non-terminal status: if the reconciler sweeper already
        # force-closed this call (or a prior shutdown finalized it), this write
        # must lose cleanly rather than overwrite the recorded outcome. Whether
        # the row actually transitioned gates the event + the bill below.
        result = await session.execute(
            update(Call)
            .where(Call.id == call_id, Call.status.not_in(TERMINAL_CALL_STATUSES))
            .values(
                status=final_status,
                end_reason=final_end_reason,
                ended_at=now,
                recording_s3_key=recording_key,
            )
        )
        transitioned = (result.rowcount or 0) > 0
        if transitioned:
            # Terminal state_change so observers see the full lifecycle, not
            # just queued→dialing. Same transaction as the status UPDATE — they
            # commit atomically. `prior_status` is whatever the row held coming
            # in (in_progress on a normal call, dialing on a no-answer/busy).
            session.add(
                CallEvent(
                    call_id=call_id,
                    kind="state_change",
                    payload={
                        "from": prior_status,
                        "to": final_status,
                        "reason": final_end_reason,
                    },
                )
            )
        # Idempotent and order-independent — free the pool number regardless of
        # which writer recorded the terminal status, so it is never leaked. In
        # the same transaction as the status update: a rollback (e.g. failed
        # usage_events insert) must also unwind the release so the sweeper
        # backstop can retry. No-op for non-pool calls.
        await release_pool_reservation(session, call_id=call_id)
        usage_event_id: str | None = None
        # Only bill for calls that *this call* actually completed a conversation
        # on. A no-answer / busy / failed call has a non-zero ring delta but
        # isn't billable; an already-terminal row (transitioned is False) was
        # billed — or deliberately not — by whoever closed it first.
        if transitioned and final_status == "completed" and duration_ms > 0:
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

    # Captured terminal status / end_reason set by the SIP-participant
    # disconnect handler below. Read by `_shutdown` to override the default
    # `"completed"` status when the call actually ended because of busy /
    # no-answer / trunk failure / etc. Mutable container so the closure
    # carries the latest value, not a snapshot.
    captured: dict[str, str | None] = {"status": None, "end_reason": None}

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        # We only care about the SIP participant — the agent (this process)
        # also fires participant_disconnected on shutdown, which we ignore.
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        status, end_reason = disconnect_reason_to_status(participant.disconnect_reason)
        if status is None:
            # CLIENT_INITIATED — normal hangup after a real conversation.
            # Leave captured as-is so `_shutdown` falls back to "completed".
            return
        captured["status"] = status
        captured["end_reason"] = end_reason
        logger.info(
            "call_id=%s sip participant disconnected — status=%s reason=%s",
            call_id,
            status,
            end_reason,
        )
        # For reasons the agent SDK does not auto-close on (notably
        # USER_UNAVAILABLE for no-answer), the session would otherwise sit
        # idle until the worker times out. Force a graceful shutdown so
        # `_shutdown` → `on_call_end` runs promptly.
        if participant.disconnect_reason not in _SDK_AUTO_CLOSE_REASONS:
            ctx.shutdown(reason=status)

    # Answer detection: flip the call to `in_progress` + stamp `answered_at`
    # the moment the SIP leg's `sip.callStatus` reaches `active` (callee
    # picked up). We watch BOTH `participant_attributes_changed` (the status
    # flips after the participant already exists) and `participant_connected`
    # (covers the case where the leg is already active when we register), plus
    # a one-shot scan of current participants — mirroring the SDK's own
    # `wait_for_participant_attribute`. A local flag dedupes; `mark_call_answered`
    # is idempotent regardless, so a missed dedupe is still safe.
    answer_tasks: set[asyncio.Task[None]] = set()
    answered = {"done": False}

    def _maybe_mark_answered(participant: rtc.Participant) -> None:
        if answered["done"] or not is_sip_answer_signal(participant):
            return
        logger.info(
            "call_id=%s sip participant answered — marking in_progress", call_id
        )

        async def _run() -> None:
            # Latch the dedupe flag only once the row actually transitioned, so
            # a signal seen a hair before the API's `dialing` commit (guard
            # no-op) is retried by a later attribute event rather than dropped.
            # `mark_call_answered` is idempotent, so the brief window in which a
            # duplicate event schedules a second task is harmless.
            if await mark_call_answered(call_id):
                answered["done"] = True

        task = asyncio.ensure_future(_run())
        answer_tasks.add(task)
        task.add_done_callback(answer_tasks.discard)

    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        _maybe_mark_answered(participant)

    @ctx.room.on("participant_attributes_changed")
    def _on_participant_attributes_changed(
        _changed: Any, participant: rtc.Participant
    ) -> None:
        _maybe_mark_answered(participant)

    # The SIP leg may already be present/active by the time handlers register.
    for _participant in ctx.room.remote_participants.values():
        _maybe_mark_answered(_participant)

    vad = ctx.proc.userdata["vad"]
    session = build_session(metadata.get("llm"), vad)
    event_tasks = attach_event_handlers(session, call_id)

    # AgentSession-level close events that aren't already covered by the SIP
    # participant_disconnected handler above: `error` (LLM/STT/TTS crash) and
    # `job_shutdown` (worker shutdown signal). Only stamp end_reason if the
    # SIP path hasn't already claimed it.
    @session.on("close")
    def _on_session_close(ev: Any) -> None:
        if captured["end_reason"] is not None:
            return
        reason = getattr(ev, "reason", None)
        if reason == "error":
            captured["end_reason"] = CallEndReason.AGENT_ERROR.value
            captured["status"] = "failed"
        elif reason == "job_shutdown":
            captured["end_reason"] = CallEndReason.WORKER_SHUTDOWN.value
            captured["status"] = "failed"

    agent = Agent(instructions=build_instructions(metadata.get("system_prompt")))
    await session.start(agent=agent, room=ctx.room)

    if metadata.get("first_message"):
        await session.say(metadata["first_message"], allow_interruptions=True)

    room_name = ctx.room.name

    soft_cap_seconds = settings.hail_voice_max_duration_seconds
    soft_cap_task: asyncio.Task[None] | None = None
    if soft_cap_seconds > 0:
        # When the cap actually fires (vs being cancelled by a natural hangup)
        # stamp end_reason='soft_cap_reached' before ctx.shutdown so the
        # shutdown callback writes it.
        def _on_soft_cap_fired() -> None:
            captured["end_reason"] = CallEndReason.SOFT_CAP_REACHED.value
            # Status stays None → on_call_end falls back to "completed".

        soft_cap_task = asyncio.create_task(
            soft_cap_announce_and_hangup(
                ctx,
                session,
                call_id,
                soft_cap_seconds,
                on_fire=_on_soft_cap_fired,
            )
        )

    async def _shutdown() -> None:
        if soft_cap_task is not None and not soft_cap_task.done():
            soft_cap_task.cancel()
            await asyncio.gather(soft_cap_task, return_exceptions=True)
        if event_tasks:
            await asyncio.gather(*list(event_tasks), return_exceptions=True)
        if answer_tasks:
            await asyncio.gather(*list(answer_tasks), return_exceptions=True)
        await on_call_end(
            call_id,
            room_name,
            status_override=captured["status"],
            end_reason_override=captured["end_reason"],
        )

    ctx.add_shutdown_callback(_shutdown)


__all__ = [
    "SIP_CALL_STATUS_ACTIVE",
    "SIP_CALL_STATUS_ATTRIBUTE",
    "SOFT_CAP_ANNOUNCEMENT",
    "SOFT_CAP_END_REASON",
    "VOICE_PREAMBLE",
    "attach_event_handlers",
    "build_instructions",
    "disconnect_reason_to_status",
    "entrypoint",
    "is_sip_answer_signal",
    "mark_call_answered",
    "on_call_end",
    "parse_metadata",
    "prewarm",
    "soft_cap_announce_and_hangup",
    "write_call_event",
]
