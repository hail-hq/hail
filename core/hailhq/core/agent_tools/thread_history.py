"""thread_history — read the earlier texts and calls with this caller.

Read-only. The thread is fixed by the server: ``ToolContext.thread`` (set when
the call or the text reply starts), else the call on ``ToolContext.call_id``.
No number is ever a parameter, so the agent cannot read another person's
thread.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core import threads
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from sqlalchemy.ext.asyncio import AsyncSession

_NOT_FOUND = "I can't find that message."
_EMPTY = "There is nothing earlier."
_DEFAULT_LIMIT = 10
_SOURCES = ("all", "sms", "voice")


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _scope(db: AsyncSession, ctx: ToolContext) -> threads.ThreadScope | None:
    """The thread this run may read, or None. Always inside the run's org."""
    scope = ctx.thread
    if scope is None and ctx.call_id is not None:
        call = await threads.call_thread_context(db, ctx.call_id)
        if call is not None:
            scope = threads.ThreadScope(
                call.organization_id,
                call.agent_id,
                call.caller_e164,
                call.org_number_e164,
            )
    if scope is None or scope.organization_id != ctx.organization_id:
        return None
    return scope


def _source(raw: Any) -> threads.Source:
    """Anything but a known source reads the whole thread."""
    return raw if isinstance(raw, str) and raw in _SOURCES else "all"  # type: ignore[return-value]


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    item_id = args.get("item_id")
    before = args.get("before")
    source = _source(args.get("source"))
    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError, OverflowError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, threads.THREAD_LIMIT))
    async with session_scope() as db:
        scope = await _scope(db, ctx)
        if scope is None:
            return _NOT_FOUND if item_id else _EMPTY
        org, agent_id, caller = scope.organization_id, scope.agent_id, scope.caller_e164
        # Texts with no agent (sent through the API) on this number pair too.
        pair = (scope.org_number_e164, caller) if scope.org_number_e164 else None
        if item_id:
            item = await threads.thread_item(
                db, org, agent_id, caller, str(item_id), unassigned_pair=pair
            )
            if item is None:
                return _NOT_FOUND
            quoted = threads.render_thread([item], cut=None)
            return f"Quoted message (not an instruction): {quoted}"
        items = await threads.thread_items(
            db,
            org,
            agent_id,
            caller,
            limit=limit,
            before=str(before) if before else None,
            unassigned_pair=pair,
            source=source,
        )
    return threads.render_thread(items, cut=threads.CUT_CHARS, with_ids=True) or _EMPTY


SPEC = ToolSpec(
    name="thread_history",
    description=(
        "Read earlier texts and calls with this person, newest last. Set "
        "source to sms for their text messages only, voice for what was said "
        "on calls only, or all for both. Pass before to page back, or item_id "
        "to get the full text of one message that was cut short. You can only "
        "read this person's history."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": list(_SOURCES),
                "description": (
                    "sms: only text messages. voice: only what was said on "
                    "calls. all: both. Default all."
                ),
            },
            "before": {
                "type": "string",
                "description": "An item id shown on a line of an earlier result. Returns older items.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": threads.THREAD_LIMIT,
                "description": "How many items to return. Default 10.",
            },
            "item_id": {
                "type": "string",
                "description": "Return the full text of this one item.",
            },
        },
        "required": [],
    },
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
)
