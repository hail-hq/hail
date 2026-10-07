"""Agent threads: one history per (organization, agent, caller number).

A thread is a query, not a table. It merges the agent's texts with the caller
(``sms``) and the spoken turns of its calls (``call_events``). The caller is
``from_e164`` on inbound rows and ``to_e164`` on outbound rows. Every read here
takes the organization, the agent and the caller from the server; nothing reads
a thread by a number the model supplied.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from hailhq.core.models import Call, CallEvent, Sms
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def is_e164(value: str | None) -> bool:
    """Withheld callers arrive as "", "anonymous", "restricted" and the like.
    Only a real E.164 number identifies a thread; anything else has none."""
    return bool(value) and _E164.fullmatch(value) is not None


__all__ = [
    "ACTIVE_CALL_MAX_AGE",
    "CUT_CHARS",
    "TEXT_MARKER",
    "THREAD_LIMIT",
    "THREAD_WINDOW",
    "ThreadItem",
    "active_call_for_thread",
    "call_thread_key",
    "render_thread",
    "thread_item",
    "thread_items",
]

THREAD_LIMIT = 30
THREAD_WINDOW = timedelta(days=7)
CUT_CHARS = 500
# A call older than this is never "active", even if its status is stuck. Above
# the 3600s duration cap plus grace; the stale-call sweep skips calls with no
# max_duration_seconds, so this bound keeps one stuck row from silencing the
# text agent for a caller forever.
ACTIVE_CALL_MAX_AGE = timedelta(hours=2)
# Prefix of the conversation item the voicebot adds when the caller texts
# during a call. That text is already an ``sms`` row, so the matching
# ``user_turn`` event is left out of the thread.
TEXT_MARKER = "[text message from caller] "

ItemKind = Literal["text_in", "text_out", "call_caller", "call_agent"]


@dataclass(frozen=True)
class ThreadItem:
    id: str  # "sms:<uuid>" or "event:<uuid>"
    at: datetime
    kind: ItemKind
    text: str


def _sms_filter(organization_id, agent_id, caller):
    return (
        Sms.organization_id == organization_id,
        Sms.agent_id == agent_id,
        or_(
            and_(Sms.direction == "inbound", Sms.from_e164 == caller),
            and_(Sms.direction == "outbound", Sms.to_e164 == caller),
        ),
    )


def _call_filter(organization_id, agent_id, caller):
    return (
        Call.organization_id == organization_id,
        Call.agent_id == agent_id,
        or_(
            and_(Call.direction == "inbound", Call.from_e164 == caller),
            and_(Call.direction == "outbound", Call.to_e164 == caller),
        ),
    )


def _sms_item(row: Sms) -> ThreadItem:
    return ThreadItem(
        id=f"sms:{row.id}",
        at=row.requested_at,
        kind="text_in" if row.direction == "inbound" else "text_out",
        text=row.body or "",
    )


def _event_item(ev: CallEvent) -> ThreadItem | None:
    text = str((ev.payload or {}).get("text", ""))
    if ev.kind == "user_turn" and text.startswith(TEXT_MARKER):
        return None
    if not text.strip():
        return None
    return ThreadItem(
        id=f"event:{ev.id}",
        at=ev.occurred_at,
        kind="call_caller" if ev.kind == "user_turn" else "call_agent",
        text=text,
    )


def _before(at_col, id_col, prefix, a_at, a_prefix, a_key):
    """SQL for: this row sorts before the anchor on ``(at, "<prefix>:<uuid>")``."""
    if prefix == a_prefix:
        tie = at_col == a_at, id_col < a_key
        return or_(at_col < a_at, and_(*tie))
    if prefix < a_prefix:
        return at_col <= a_at
    return at_col < a_at


async def thread_items(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
    *,
    limit: int = THREAD_LIMIT,
    before: str | None = None,
    until: datetime | None = None,
    unassigned_pair: tuple[str, str] | None = None,
) -> list[ThreadItem]:
    """The last ``limit`` items of the thread inside ``THREAD_WINDOW``, oldest
    first. ``before`` is an item id: only items older than it come back, by
    (time, id) order. ``until`` drops items after that time and must be
    timezone-aware. ``unassigned_pair`` is ``(org number, caller)``: it also
    brings in texts of the organization with no agent (sent through the API)
    between those two numbers, either direction. Only the text agent asks."""
    if not is_e164(caller_e164):
        return []
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    anchor = None
    if before is not None:
        anchor = await thread_item(db, organization_id, agent_id, caller_e164, before)
        if anchor is None:
            return []

    sms_where = and_(*_sms_filter(organization_id, agent_id, caller_e164))
    if unassigned_pair is not None:
        org_number, pair_caller = unassigned_pair
        if is_e164(org_number) and pair_caller == caller_e164:
            sms_where = or_(
                sms_where,
                and_(
                    Sms.organization_id == organization_id,
                    Sms.agent_id.is_(None),
                    or_(
                        and_(Sms.from_e164 == caller_e164, Sms.to_e164 == org_number),
                        and_(Sms.from_e164 == org_number, Sms.to_e164 == caller_e164),
                    ),
                ),
            )
    sms_stmt = select(Sms).where(sms_where).where(Sms.requested_at >= since)
    # Injected-text turns and blank turns are excluded in SQL, so ``limit``
    # counts only items that can be returned.
    text = func.coalesce(CallEvent.payload["text"].astext, "")
    ev_stmt = (
        select(CallEvent)
        .join(Call, Call.id == CallEvent.call_id)
        .where(*_call_filter(organization_id, agent_id, caller_e164))
        .where(CallEvent.kind.in_(("user_turn", "agent_turn")))
        .where(CallEvent.occurred_at >= since)
        .where(func.btrim(text) != "")
        .where(
            ~and_(
                CallEvent.kind == "user_turn",
                text.startswith(TEXT_MARKER, autoescape=True),
            )
        )
    )
    # Cut at the anchor by (time, id), the same key the result is sorted by.
    # Ties on time are broken by the id prefix, then by the uuid.
    if anchor is not None:
        a_prefix, _, a_raw = anchor.id.partition(":")
        a_key = uuid.UUID(a_raw)
        sms_stmt = sms_stmt.where(
            _before(Sms.requested_at, Sms.id, "sms", anchor.at, a_prefix, a_key)
        )
        ev_stmt = ev_stmt.where(
            _before(
                CallEvent.occurred_at, CallEvent.id, "event", anchor.at, a_prefix, a_key
            )
        )
    if until is not None:
        sms_stmt = sms_stmt.where(Sms.requested_at <= until)
        ev_stmt = ev_stmt.where(CallEvent.occurred_at <= until)
    sms_stmt = sms_stmt.order_by(Sms.requested_at.desc(), Sms.id.desc()).limit(limit)
    ev_stmt = ev_stmt.order_by(CallEvent.occurred_at.desc(), CallEvent.id.desc()).limit(
        limit
    )
    items = [_sms_item(r) for r in (await db.execute(sms_stmt)).scalars()]
    for ev in (await db.execute(ev_stmt)).scalars():
        item = _event_item(ev)
        if item is not None:
            items.append(item)
    items.sort(key=lambda i: (i.at, i.id))
    if anchor is not None:
        items = [i for i in items if (i.at, i.id) < (anchor.at, anchor.id)]
    return items[-limit:]


async def thread_item(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
    item_id: str,
) -> ThreadItem | None:
    """One item, only if it belongs to this thread. An explicit id ignores
    ``THREAD_WINDOW``: older items of the same thread can be read by id."""
    if not is_e164(caller_e164):
        return None
    kind, _, raw = item_id.partition(":")
    try:
        key = uuid.UUID(raw)
    except ValueError:
        return None
    if kind == "sms":
        row = (
            await db.execute(
                select(Sms).where(
                    Sms.id == key, *_sms_filter(organization_id, agent_id, caller_e164)
                )
            )
        ).scalar_one_or_none()
        return _sms_item(row) if row is not None else None
    if kind == "event":
        ev = (
            await db.execute(
                select(CallEvent)
                .join(Call, Call.id == CallEvent.call_id)
                .where(
                    CallEvent.id == key,
                    CallEvent.kind.in_(("user_turn", "agent_turn")),
                    *_call_filter(organization_id, agent_id, caller_e164),
                )
            )
        ).scalar_one_or_none()
        return _event_item(ev) if ev is not None else None
    return None


async def call_thread_key(
    db: AsyncSession, call_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """``(organization_id, agent_id, caller_e164)`` for a call, or None when no
    agent is on it or the caller number is not E.164 (withheld). The caller is
    the person on the line."""
    call = await db.get(Call, call_id)
    if call is None or call.agent_id is None:
        return None
    caller = call.from_e164 if call.direction == "inbound" else call.to_e164
    if not is_e164(caller):
        return None
    return call.organization_id, call.agent_id, caller


async def active_call_for_thread(
    db: AsyncSession,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID,
    caller_e164: str,
) -> Call | None:
    """The newest dialing, ringing or in-progress call of this thread created within
    ``ACTIVE_CALL_MAX_AGE``, or None."""
    if not is_e164(caller_e164):
        return None
    since = datetime.now(timezone.utc) - ACTIVE_CALL_MAX_AGE
    stmt = (
        select(Call)
        .where(*_call_filter(organization_id, agent_id, caller_e164))
        .where(Call.status.in_(("dialing", "ringing", "in_progress")))
        .where(Call.created_at >= since)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


_LABEL: dict[str, str] = {
    "text_in": "text from caller",
    "text_out": "text from you",
    "call_caller": "caller",
    "call_agent": "you",
}


def render_thread(
    items: list[ThreadItem],
    *,
    cut: int | None = CUT_CHARS,
    with_ids: bool = False,
) -> str:
    """Plain-text lines for a prompt. ``cut`` caps one item's text;
    ``with_ids`` ends every line with the item id (for paging)."""
    lines: list[str] = []
    for item in items:
        # One item is one line: a text cannot forge further entries.
        text = " ".join(item.text.strip().splitlines())
        if cut is not None and len(text) > cut:
            text = (
                f"{text[:cut]}... (cut, ask thread_history with "
                f'item_id "{item.id}" for the full text)'
            )
        if with_ids:
            text = f'{text} [item_id "{item.id}"]'
        stamp = item.at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if item.kind.startswith("text"):
            lines.append(f"[{stamp}] {_LABEL[item.kind]}: {text}")
        else:
            lines.append(f"[{stamp}] on a call, {_LABEL[item.kind]}: {text}")
    return "\n".join(lines)
