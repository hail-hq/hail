"""ToolSpec / ToolContext — the contract between core tools and the voicebot.

``execute`` returns a short plain sentence the agent speaks. Expected
failures (unavailable channel, denied send) come back as speakable
sentences, not exceptions; the voicebot wrapper catches anything else.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.client import AgentApiClient

RiskTier = Literal["read_only", "session_control", "outbound_send"]


@dataclass
class ToolContext:
    """Capability handles the voicebot supplies per call.

    ``api`` is None when HAIL_INTERNAL_SECRET is unset (send tools are
    unavailable then); ``hangup`` is None outside a live session.
    """

    call_id: uuid.UUID
    organization_id: uuid.UUID
    api: AgentApiClient | None
    hangup: Callable[[], Awaitable[None]] | None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, type: object
    risk_tier: RiskTier
    is_available: Callable[[uuid.UUID, AsyncSession], Awaitable[bool]]
    execute: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


__all__ = ["RiskTier", "ToolContext", "ToolSpec"]
