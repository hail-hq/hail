"""hail_settings_help: the spoken guide to Hail settings, loaded on demand.

Not in ``all_tools()``, which feeds the names a customer may allow-list.
``call_tools()`` includes it, and ``on_every_call`` keeps it on every call
whatever the ``tools`` list says. ``members_only`` keeps it to calls with a
workspace member on the line: a business's customers never hear Hail's setup
steps.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.prompts import VOICE_CONFIGURATION_HELP
from sqlalchemy.ext.asyncio import AsyncSession


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(_ctx: ToolContext, _args: dict[str, Any]) -> str:
    return VOICE_CONFIGURATION_HELP


SPEC = ToolSpec(
    name="hail_settings_help",
    description=(
        "Get current Hail settings guidance for voice, greeting, language, "
        "instructions and AI disclosure. Call it before answering whether "
        "the AI introduction is always required, whether it can be disabled, "
        "or where to change these settings for a saved agent, workspace or "
        "one-off call. Never say this tool's name aloud."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
    on_every_call=True,
    members_only=True,
)
