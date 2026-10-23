"""end_call: hang up gracefully once the call's goal is met.

session_control tier: purely local (the voicebot's ``hangup`` handle),
no API call, affects only its own call. The voicebot wrapper waits for
the agent's pre-tool speech to finish playing before this runs.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from sqlalchemy.ext.asyncio import AsyncSession


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.hangup is None:
        return "I can't end the call right now."
    await ctx.hangup()
    return "Call ended."


SPEC = ToolSpec(
    name="end_call",
    description=(
        "End this phone call. Use only when the other party says they need "
        "nothing more, says goodbye, or asks to end the call. A thank-you "
        "alone is not a goodbye: ask if there is anything else. Say your "
        "goodbye out loud BEFORE calling this tool; it hangs up right after. "
        "If they start talking during your goodbye, the call stays open: "
        "listen and answer them."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_execute,
)
