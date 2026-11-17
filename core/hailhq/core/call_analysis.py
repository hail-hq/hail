"""Call transcripts and the ``call.analyzed`` webhook (DB side).

A phone call's transcript is its ``user_turn`` and ``agent_turn`` rows in
``call_events``, written by the voicebot while the call runs.
``GET /v1/calls/{id}`` and the webhook both read it with
:func:`call_transcript`.

The voicebot's analysis worker (``hailhq.voicebot.callsummary``) polls
:func:`next_call_to_analyze`, asks the LLM for a summary with no DB session
open, then calls :func:`send_call_analyzed`. That last step locks the call
row, writes one ``analyzed`` call event (the marker, so a call is sent once)
and enqueues the ``call.analyzed`` deliveries in the same transaction. Only
calls of an org with an active ``call.analyzed`` subscription are picked, so
no model runs for an org that does not listen.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from hailhq.core.models import Call, CallEvent, WebhookSubscription
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.core.threads import TEXT_MARKER
from hailhq.core.webhook_events import WebhookEventType
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from sqlalchemy import any_, exists, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ANALYSIS_WINDOW",
    "ANALYZED_KIND",
    "SETTLE_DELAY",
    "call_analyzed_data",
    "call_duration_seconds",
    "call_transcript",
    "next_call_to_analyze",
    "send_call_analyzed",
]

# ``call_events.kind`` of the marker row: the call's call.analyzed step ran.
ANALYZED_KIND = "analyzed"
TURN_KINDS = ("user_turn", "agent_turn")
# Only calls that ended this recently are analyzed: a new subscription (or
# the first deploy) never sends events for old calls.
ANALYSIS_WINDOW = timedelta(hours=1)
# Wait this long after the end, so the last turn rows are written first.
SETTLE_DELAY = timedelta(seconds=10)


async def call_transcript(db: AsyncSession, call_id: uuid.UUID) -> list[dict[str, Any]]:
    """The call's spoken turns, oldest first: ``{"role", "text", "at"}``.

    ``role`` is ``assistant`` for the agent and ``user`` for the other side.
    Blank turns are left out, and so are texts the caller sent during the
    call (they are SMS rows already, see ``threads.TEXT_MARKER``)."""
    rows = (
        await db.execute(
            select(CallEvent)
            .where(CallEvent.call_id == call_id, CallEvent.kind.in_(TURN_KINDS))
            .order_by(CallEvent.occurred_at, CallEvent.id)
        )
    ).scalars()
    turns: list[dict[str, Any]] = []
    for ev in rows:
        text = str((ev.payload or {}).get("text") or "")
        if not text.strip():
            continue
        if ev.kind == "user_turn" and text.startswith(TEXT_MARKER):
            continue
        turns.append(
            {
                "role": "user" if ev.kind == "user_turn" else "assistant",
                "text": text,
                "at": ev.occurred_at.isoformat(),
            }
        )
    return turns


def call_duration_seconds(call: Call) -> int | None:
    """Seconds from answer to end, the time a call is billed for. None when
    the call was never answered or has not ended."""
    if call.answered_at is None or call.ended_at is None:
        return None
    return max(0, round((call.ended_at - call.answered_at).total_seconds()))


def call_analyzed_data(
    call: Call, transcript: list[dict[str, Any]], summary: str | None
) -> dict[str, Any]:
    """``data`` of a ``call.analyzed`` event: the ``call.*`` fields plus the
    duration, the transcript and the summary (null when it failed)."""
    return call_event_data(
        call,
        duration_seconds=call_duration_seconds(call),
        transcript=transcript,
        summary=summary,
    )


def _analyzed(call_id_col: Any) -> Any:
    return exists().where(
        CallEvent.call_id == call_id_col, CallEvent.kind == ANALYZED_KIND
    )


async def next_call_to_analyze(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    exclude: set[uuid.UUID] | None = None,
) -> Call | None:
    """The oldest ended phone call that still needs its ``call.analyzed``
    event, or None. It ended between ``ANALYSIS_WINDOW`` and
    ``SETTLE_DELAY`` ago, has at least one turn row, has no ``analyzed``
    marker, and its org has an active subscription to ``call.analyzed``.
    Takes no lock: :func:`send_call_analyzed` decides who sends."""
    now = now or datetime.now(timezone.utc)
    # The subscriptions table is small: with no listener, calls is not read.
    orgs = (
        await db.scalars(
            select(WebhookSubscription.organization_id)
            .where(
                WebhookSubscription.status == "active",
                literal(WebhookEventType.CALL_ANALYZED.value)
                == any_(WebhookSubscription.event_types),
            )
            .distinct()
        )
    ).all()
    if not orgs:
        return None
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
            has_turn,
            ~_analyzed(Call.id),
        )
        .order_by(Call.ended_at, Call.id)
        .limit(1)
    )
    if exclude:
        stmt = stmt.where(Call.id.not_in(exclude))
    return (await db.execute(stmt)).scalar_one_or_none()


async def send_call_analyzed(
    db: AsyncSession, call_id: uuid.UUID, summary: str | None
) -> bool:
    """Write the ``analyzed`` marker and enqueue the ``call.analyzed``
    deliveries, once per call, then commit. Returns True when deliveries
    were enqueued. A call already marked, or one whose transcript is empty
    after filtering, gets no event (the latter still gets the marker, so it
    is not picked again)."""
    call = (
        await db.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None or await db.scalar(select(_analyzed(call_id))):
        await db.rollback()
        return False
    transcript = await call_transcript(db, call_id)
    db.add(
        CallEvent(
            call_id=call_id,
            kind=ANALYZED_KIND,
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
            event_type=WebhookEventType.CALL_ANALYZED.value,
            event_id=call.id,
            data=call_analyzed_data(call, transcript, summary),
        )
    await db.commit()
    return sent > 0
