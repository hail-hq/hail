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

from hailhq.core.agent_tools.client import AgentApiClient
from sqlalchemy.ext.asyncio import AsyncSession

RiskTier = Literal["read_only", "session_control", "outbound_send"]

# Shared spoken fallback when the internal route's response carries no
# ``spoken`` text (defensive — the route always sets one today). Both send
# tools and the voicebot's own tool-failure path use this exact string so
# the callee hears one consistent apology regardless of where it originates.
SPOKEN_FALLBACK = "Sorry, that didn't work."


@dataclass
class ToolContext:
    """Capability handles the voicebot supplies per call.

    ``api`` is None when HAIL_INTERNAL_SECRET is unset (send tools are
    unavailable then); ``hangup`` and ``send_dtmf`` are None outside a live
    session.

    ``send_dtmf`` takes the already-validated digit string and publishes it to
    the SIP leg. Keeping it a handle (rather than importing ``livekit`` here)
    is what keeps ``core`` free of transport dependencies.
    """

    call_id: uuid.UUID
    organization_id: uuid.UUID
    api: AgentApiClient | None
    hangup: Callable[[], Awaitable[None]] | None
    send_dtmf: Callable[[str], Awaitable[None]] | None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, type: object
    risk_tier: RiskTier
    is_available: Callable[[uuid.UUID, AsyncSession], Awaitable[bool]]
    execute: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


__all__ = ["SPOKEN_FALLBACK", "RiskTier", "ToolContext", "ToolSpec"]
