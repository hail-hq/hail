"""Call transcripts and the ``call.transcribed`` webhook (DB side).

A phone call's transcript is its ``user_turn`` and ``agent_turn`` rows in
``call_events``, written by the voicebot while the call runs.
``GET /v1/calls/{id}`` and the webhook both read it with
:func:`call_transcript`.

The voicebot's analysis worker (``hailhq.voicebot.callsummary``) calls
:func:`claim_call_to_analyze`, which writes a ``transcribing`` call event (the
claim, so only one worker runs the model for a call), then asks the LLM for a
summary with no DB session open, then calls :func:`send_call_transcribed`. That
last step locks the call row, writes one ``transcribed`` call event (the marker,
so a call is sent once) and enqueues the ``call.transcribed`` deliveries in the
same transaction. Only calls of an org with an active ``call.transcribed``
subscription are picked, so no model runs for an org that does not listen.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hailhq.core.models import Call, CallEvent, WebhookSubscription
from hailhq.core.schemas import TERMINAL_CALL_STATUSES, CallTranscriptTurn
from hailhq.core.threads import spoken_text
from hailhq.core.webhook_events import WebhookEventType
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from sqlalchemy import any_, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ANALYSIS_WINDOW",
    "CLAIM_KIND",
    "CLAIM_LEASE",
    "SETTLE_DELAY",
    "TRANSCRIBED_KIND",
    "call_duration_seconds",
    "call_transcribed_data",
    "call_transcript",
    "claim_call_to_analyze",
    "send_call_transcribed",
]

# ``call_events.kind`` of the marker row: the call's call.transcribed step ran.
TRANSCRIBED_KIND = "transcribed"
# ``call_events.kind`` of a claim: a worker is summarizing the call. Counts
# only while younger than ``CLAIM_LEASE``.
CLAIM_KIND = "transcribing"
# Longer than the model timeout (30 s) plus the config and transcript reads.
# A worker that dies mid-summary frees the call when this runs out.
CLAIM_LEASE = timedelta(minutes=5)
TURN_KINDS = ("user_turn", "agent_turn")
# A call not sent this long after its end is dropped (a backlog, or the
# voicebot down). The other limits: ``claim_call_to_analyze`` reads only
# calls created in the last ANALYSIS_WINDOW plus a day (an index bound; no
# call runs a day), and its subscription rule keeps out calls that ended
# before a subscription was created or changed.
ANALYSIS_WINDOW = timedelta(hours=24)
# Wait this long after the end, so the last turn rows are written first.
SETTLE_DELAY = timedelta(seconds=10)


def _spoken_at(ev: CallEvent) -> datetime:
    """When the turn started: LiveKit's ``started_speaking_at`` (epoch
    seconds, set by the voicebot in ``agent.attach_event_handlers``), else
    when the row was written.

    Rows are written by async tasks, so on a barge-in the interrupted agent
    turn and the caller's turn can be written in either order; the speech
    start keeps them in the order they were said. Limit: a row without the
    key falls back to its write time, which comes after the turn ended (the
    turn's length plus the speech-to-text delay, often seconds), and is the
    DB clock, not the voicebot host's. Such a turn can sort after a turn
    that started later. LiveKit sets the key on spoken user and agent
    turns, so this is rare."""
    value = (ev.payload or {}).get("started_speaking_at")
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    return ev.occurred_at


async def call_transcript(db: AsyncSession, call_id: uuid.UUID) -> list[dict[str, Any]]:
    """The call's spoken turns, in the order they were said:
    ``{"role", "text", "at"}``, where ``at`` is when the turn started
    (:func:`_spoken_at`).

    ``role`` is ``assistant`` for the agent and ``user`` for the other side.
    Blank turns are left out, and so are texts the caller sent during the
    call (they are SMS rows already, see ``threads.spoken_text``)."""
    rows = (
        await db.execute(
            select(CallEvent)
            .where(CallEvent.call_id == call_id, CallEvent.kind.in_(TURN_KINDS))
            .order_by(CallEvent.occurred_at, CallEvent.id)
        )
    ).scalars()
    turns: list[CallTranscriptTurn] = []
    for ev in rows:
        text = spoken_text(ev)
        if text is None:
            continue
        turns.append(
            CallTranscriptTurn(
                role="user" if ev.kind == "user_turn" else "assistant",
                text=text,
                at=_spoken_at(ev),
            )
        )
    # Stable: equal start times keep the write order from the query.
    turns.sort(key=lambda turn: turn.at)
    # JSON form, so the webhook and GET /v1/calls/{id} print ``at`` alike.
    return [turn.model_dump(mode="json") for turn in turns]


def call_duration_seconds(call: Call) -> int | None:
    """Seconds from answer to end, the time a call is billed for. None when
    the call was never answered or has not ended."""
    if call.answered_at is None or call.ended_at is None:
        return None
    return max(0, round((call.ended_at - call.answered_at).total_seconds()))


def call_transcribed_data(
    call: Call, transcript: list[dict[str, Any]], summary: str | None
) -> dict[str, Any]:
    """``data`` of a ``call.transcribed`` event: the ``call.*`` fields plus the
    duration, the transcript and the summary (null when it failed)."""
    return call_event_data(
        call,
        duration_seconds=call_duration_seconds(call),
        transcript=transcript,
        summary=summary,
    )


def _transcribed(call_id_col: Any) -> Any:
    return exists().where(
        CallEvent.call_id == call_id_col, CallEvent.kind == TRANSCRIBED_KIND
    )


def _claimed(call_id_col: Any) -> Any:
    return exists().where(
        CallEvent.call_id == call_id_col,
        CallEvent.kind == CLAIM_KIND,
        CallEvent.occurred_at > func.now() - CLAIM_LEASE,
    )


# Tries per claim call when another worker wins the re-check, so a lost race
# moves on to the next call instead of waiting a poll.
_CLAIM_TRIES = 3


async def claim_call_to_analyze(
    db: AsyncSession,
    *,
    exclude: set[uuid.UUID] | None = None,
) -> Call | None:
    """Claim the oldest ended phone call that still needs its
    ``call.transcribed`` event, and commit. None when no call waits.

    The call ended between ``ANALYSIS_WINDOW`` and ``SETTLE_DELAY`` ago, has
    at least one turn row, has no ``transcribed`` marker and no live claim, and
    its org has an active subscription to ``call.transcribed`` last created or
    changed before the call ended (so a new, edited or re-enabled
    subscription never gets events for older calls).

    Only one worker gets a call, across threads and voicebot replicas: the
    call row is locked ``FOR NO KEY UPDATE SKIP LOCKED``, checked again, and an
    ``transcribing`` row is written and committed before the slow model call.
    A claim older than ``CLAIM_LEASE`` no longer counts, so a crashed worker
    does not drop the event. :func:`send_call_transcribed` still checks the
    marker, so a worker whose lease ran out cannot send a second event.
    "Now" here is the DB clock (``now()``). Claims are written and aged on
    that clock, so host clock skew does not move the lease. A subscription's
    ``updated_at`` is the DB clock too (``statement_timestamp()`` on create,
    PATCH and rotation).
    ``ended_at`` is the voicebot or api host clock (the DB's ``now()`` for a
    sweeper-ended call), so the windows and the ``updated_at <= ended_at``
    check can move by that host's skew: a call that ends within the skew of
    a subscription change can fall on either side."""
    now = func.now()
    # The subscriptions table is small: with no listener, calls is not read.
    orgs = (
        await db.scalars(
            select(WebhookSubscription.organization_id)
            .where(
                WebhookSubscription.status == "active",
                literal(WebhookEventType.CALL_TRANSCRIBED.value)
                == any_(WebhookSubscription.event_types),
            )
            .distinct()
        )
    ).all()
    if not orgs:
        return None
    listening = exists().where(
        WebhookSubscription.organization_id == Call.organization_id,
        WebhookSubscription.status == "active",
        # Set on create and by every PATCH (event types, status, URL) or
        # secret rotation, never by the delivery worker: adding the event
        # or re-enabling a subscription does not replay past calls.
        WebhookSubscription.updated_at <= Call.ended_at,
        literal(WebhookEventType.CALL_TRANSCRIBED.value)
        == any_(WebhookSubscription.event_types),
    )
    has_turn = exists().where(
        CallEvent.call_id == Call.id, CallEvent.kind.in_(TURN_KINDS)
    )
    stmt = (
        select(Call)
        .where(
            Call.organization_id.in_(orgs),
            # No call runs a day, so this bound only lets the (org, created)
            # index skip older rows.
            Call.created_at >= now - ANALYSIS_WINDOW - timedelta(days=1),
            Call.transport == "phone",
            Call.status.in_(TERMINAL_CALL_STATUSES),
            Call.ended_at >= now - ANALYSIS_WINDOW,
            Call.ended_at <= now - SETTLE_DELAY,
            listening,
            has_turn,
            ~_transcribed(Call.id),
            ~_claimed(Call.id),
        )
        .order_by(Call.ended_at, Call.id)
        .limit(1)
        # FOR NO KEY UPDATE: serializes claimers without blocking inserts of
        # rows that reference the call (turns, usage).
        .with_for_update(of=Call, skip_locked=True, key_share=True)
    )
    skip = set(exclude or ())
    for _ in range(_CLAIM_TRIES):
        picked = stmt.where(Call.id.not_in(skip)) if skip else stmt
        call = (await db.execute(picked)).scalar_one_or_none()
        if call is None:
            await db.rollback()
            return None
        call_id = call.id  # a rollback expires ``call``
        # Check again in a new statement. Under READ COMMITTED the select
        # above read a snapshot taken before it got the lock, so a claim or
        # marker that another worker committed in between is seen only here.
        if await db.scalar(select(or_(_transcribed(call_id), _claimed(call_id)))):
            await db.rollback()
            skip.add(call_id)
            continue
        # Worker job state: GET /v1/events leaves these rows out. Its
        # occurred_at is the DB's now(), the clock the lease is read on.
        db.add(CallEvent(call_id=call_id, kind=CLAIM_KIND, payload={}))
        await db.commit()
        return call
    return None


async def send_call_transcribed(
    db: AsyncSession,
    call_id: uuid.UUID,
    transcript: list[dict[str, Any]],
    summary: str | None,
) -> bool:
    """Write the ``transcribed`` marker and enqueue the ``call.transcribed``
    deliveries, once per call, then commit. ``transcript`` is the one the
    summary was written from (:func:`call_transcript`). Returns True when
    deliveries were enqueued. A call already marked, or one with an empty
    transcript, gets no event (the latter still gets the marker, so it is
    not picked again)."""
    call = (
        await db.execute(
            select(Call).where(Call.id == call_id).with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if call is None or await db.scalar(select(_transcribed(call_id))):
        await db.rollback()
        return False
    db.add(
        CallEvent(
            call_id=call_id,
            kind=TRANSCRIBED_KIND,
            # Counts only: the summary and the text stay out of the event
            # stream (GET /v1/events returns every call_events row).
            payload={
                "turns": len(transcript),
                "summary": summary is not None,
            },
        )
    )
    sent = 0
    if transcript:
        sent = await fanout_call_event(
            db,
            organization_id=call.organization_id,
            event_type=WebhookEventType.CALL_TRANSCRIBED.value,
            event_id=call.id,
            data=call_transcribed_data(call, transcript, summary),
            # The same rule as the pick (``listening``): a subscription
            # made or changed after the call ended gets no event for it.
            changed_before=call.ended_at,
        )
    await db.commit()
    return sent > 0
