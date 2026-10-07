"""thread_history — read the earlier texts and calls with this caller.

Read-only. The caller is found from the call on the server; no number is ever
a parameter, so the agent cannot read another person's thread.
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


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    item_id = args.get("item_id")
    before = args.get("before")
    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError, OverflowError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, threads.THREAD_LIMIT))
    async with session_scope() as db:
        key = await threads.call_thread_key(db, ctx.call_id)
        if key is None or key[0] != ctx.organization_id:
            return _NOT_FOUND if item_id else _EMPTY
        org, agent_id, caller = key
        if item_id:
            item = await threads.thread_item(db, org, agent_id, caller, str(item_id))
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
        )
    return threads.render_thread(items, cut=threads.CUT_CHARS, with_ids=True) or _EMPTY


SPEC = ToolSpec(
    name="thread_history",
    description=(
        "Read earlier texts and calls with the person on this call. Use it for "
        "older messages, or pass item_id to get the full text of one message "
        "that was cut short. You can only read this person's history."
    ),
    parameters={
        "type": "object",
        "properties": {
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
