"""wait_for_other_party: stay quiet while the other party checks something.

Not in ``all_tools()``, which feeds the names a customer may allow-list.
``call_tools()`` includes it, and ``on_every_call`` keeps it on every call:
any call can be asked to hold. The voicebot's ``hold`` handle pauses its
silence prompts until the other party speaks again
(``voicebot/hailhq/voicebot/hold.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from sqlalchemy.ext.asyncio import AsyncSession

NAME = "wait_for_other_party"


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, _args: dict[str, Any]) -> None:
    """Returns None: no reply turn, so the agent stays silent."""
    if ctx.hold is not None:
        await ctx.hold()


SPEC = ToolSpec(
    name=NAME,
    description=(
        "Wait quietly while the other party checks or looks something up. "
        "Use it when they ask you to hold or wait, or say they need a moment, "
        'in any language. Say one short line first, like "Sure, take your '
        'time", then call this tool. You then stay silent until they speak '
        "again. Not for automated phone menus. Never end the call while "
        "waiting. Never say this tool's name aloud."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_execute,
    waits_for_speech=False,
    on_every_call=True,
)
