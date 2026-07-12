"""list_contacts — read-only directory listing.

Returns names and channel presence only; raw addresses never reach the
LLM (they could be read aloud or leak into the stored transcript).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from hailhq.core.directory import list_directory


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> str:
    async with session_scope() as session:
        entries = await list_directory(session, ctx.organization_id)
    if not entries:
        return "There are no contacts available."
    lines = []
    for e in entries:
        channels = [
            label
            for label, present in (("email", e.has_email), ("text", e.has_phone))
            if present
        ]
        lines.append(f"{e.name} (reachable by {' and '.join(channels)})")
    return "Available contacts: " + "; ".join(lines) + "."


SPEC = ToolSpec(
    name="list_contacts",
    description=(
        "List the people you may send messages to: members of the "
        "organization that set up this call. Shows names and whether each "
        "person can receive email or text — never their actual addresses."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
)
