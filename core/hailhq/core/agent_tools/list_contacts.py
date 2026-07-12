"""list_contacts — read-only directory listing.

Returns names and channel presence only; raw addresses never reach the
LLM (they could be read aloud or leak into the stored transcript).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from hailhq.core.directory import list_directory

_NO_CONTACTS = "There are no contacts available."


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> str:
    try:
        async with session_scope() as session:
            entries = await list_directory(session, ctx.organization_id)
    except ProgrammingError:
        # Self-host posture: `users`/`members` are website-owned tables that
        # a pure self-host deployment never creates. Their absence means
        # there is no directory to show, not a server error — degrade to an
        # empty-directory answer instead of raising UndefinedTable on every
        # call.
        return _NO_CONTACTS
    if not entries:
        return _NO_CONTACTS
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
