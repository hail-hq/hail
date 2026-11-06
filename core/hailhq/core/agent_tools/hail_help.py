"""hail_settings_help: the spoken guide to Hail settings, loaded on demand.

Not in the registry: the registry feeds the names a customer may allow-list.
The voicebot adds this tool to every inbound call (see
``voicebot/hailhq/voicebot/tools.py``), like ``cancel_transfer``.
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
        "Get the steps for changing this agent's voice, greeting, language or "
        "instructions in the Hail console. Call it when the person asks how to "
        "change or configure any of these. Never say this tool's name aloud."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="read_only",
    is_available=_always,
    execute=_execute,
)
