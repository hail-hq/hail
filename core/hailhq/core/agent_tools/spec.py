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
from hailhq.core.threads import ThreadScope
from sqlalchemy.ext.asyncio import AsyncSession

RiskTier = Literal["read_only", "session_control", "outbound_send"]

# Shared spoken fallback when the internal route's response carries no
# ``spoken`` text (defensive — the route always sets one today). Both send
# tools and the voicebot's own tool-failure path use this exact string so
# the callee hears one consistent apology regardless of where it originates.
SPOKEN_FALLBACK = "Sorry, that didn't work."
# A call-scoped tool run with no call (the text agent).
NO_CALL = "I can do that only during a call."


@dataclass(frozen=True)
class BridgeRoute:
    to_e164: str
    from_e164: str
    trunk_id: str
    headers: dict[str, str] | None
    name: str
    reason: str
    # Called with ring_ms the moment the contact picks up, before the intro
    # plays. Returns the post that records the answer; the bridge runs it in
    # the background so a slow API never delays the intro.
    on_answered: Callable[[int], Awaitable[None]] | None = None


@dataclass(frozen=True)
class BridgeOutcome:
    outcome: Literal["answered", "no_answer", "busy", "failed"]
    sip_status: int | None
    ring_ms: int


@dataclass
class ToolContext:
    """Capability handles the voicebot supplies per call.

    ``api`` is None when HAIL_INTERNAL_SECRET is unset (send tools are
    unavailable then); ``hangup`` and ``send_dtmf`` are None outside a live
    session.

    ``send_dtmf`` takes the already-validated digit string and publishes it to
    the SIP leg. Keeping it a handle (rather than importing ``livekit`` here)
    is what keeps ``core`` free of transport dependencies.

    ``call_id`` is None outside a call (the text agent); tools that act on
    a call refuse then. ``thread`` is the one thread ``thread_history`` may
    read, set by the server when the run starts.
    """

    call_id: uuid.UUID | None
    organization_id: uuid.UUID
    api: AgentApiClient | None
    hangup: Callable[[], Awaitable[None]] | None
    send_dtmf: Callable[[str], Awaitable[None]] | None
    thread: ThreadScope | None = None
    bridge: Callable[[BridgeRoute], Awaitable[BridgeOutcome]] | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema, type: object
    risk_tier: RiskTier
    is_available: Callable[[uuid.UUID, AsyncSession], Awaitable[bool]]
    execute: Callable[[ToolContext, dict[str, Any]], Awaitable[str]]
    # Per-call shaping from dispatch metadata (names in the description,
    # enum of choices). None result hides the tool for this call.
    bind: Callable[[dict[str, Any]], ToolSpec | None] | None = None


__all__ = [
    "NO_CALL",
    "SPOKEN_FALLBACK",
    "BridgeOutcome",
    "BridgeRoute",
    "RiskTier",
    "ToolContext",
    "ToolSpec",
]
