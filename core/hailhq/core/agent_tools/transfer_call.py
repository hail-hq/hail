"""transfer_call — hand the live call to a person the admin picked.

session_control tier: the agent finishes its sentence first. The LLM only
sees names and notes (dispatch metadata ``handover_targets``); the API
resolves and re-checks the number (``/internal/agent/handover``), the
voicebot's ``bridge`` handle dials it into the room, and the API records
the outcome (``/internal/agent/handover-result``).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Any

from hailhq.core.agent_tools.spec import (
    SPOKEN_FALLBACK,
    BridgeOutcome,
    BridgeRoute,
    ToolContext,
    ToolSpec,
)
from sqlalchemy.ext.asyncio import AsyncSession

MAX_REASON_CHARS = 200

_log = logging.getLogger("hailhq.core.agent_tools")

_UNAVAILABLE = "I can't connect you to anyone right now."
_CONNECTED = "Connected."
_NO_ANSWER = "They could not pick up. Offer to take a message."


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    # The static spec is only for allowlists; bind() decides per call.
    return True


async def _unbound(_ctx: ToolContext, _args: dict[str, Any]) -> str:
    return _UNAVAILABLE


SPEC = ToolSpec(
    name="transfer_call",
    description="Connect the caller to a person on the team.",
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_unbound,
)


def bind(metadata: dict[str, Any]) -> ToolSpec | None:
    raw = metadata.get("handover_targets") or []
    if not isinstance(raw, list):
        return None
    targets = [
        t for t in raw if isinstance(t, dict) and t.get("label") and t.get("contact_id")
    ]
    if not targets:
        return None
    by_label = {t["label"]: t["contact_id"] for t in targets}
    menu = "; ".join(f"{t['label']} ({t.get('note') or ''})" for t in targets)

    async def execute(ctx: ToolContext, args: dict[str, Any]) -> str:
        if ctx.api is None or ctx.bridge is None:
            return _UNAVAILABLE
        label = str(args.get("contact", ""))
        contact_id = by_label.get(label)
        if contact_id is None:
            return "I can only connect you to: " + ", ".join(by_label) + "."
        reason = " ".join(str(args.get("reason", "")).split())[:MAX_REASON_CHARS]
        route = await ctx.api.post(
            "/internal/agent/handover",
            {"call_id": str(ctx.call_id), "contact_id": contact_id},
        )
        if not route.get("ok"):
            return str(route.get("spoken") or SPOKEN_FALLBACK)
        try:
            outcome = await ctx.bridge(
                BridgeRoute(
                    to_e164=route["to_e164"],
                    from_e164=route["from_e164"],
                    trunk_id=route["trunk_id"],
                    headers=route.get("headers"),
                    name=label,
                    reason=reason,
                )
            )
        except Exception:
            _log.exception("handover bridge failed")
            outcome = BridgeOutcome("failed", None, 0)
        try:
            await ctx.api.post(
                "/internal/agent/handover-result",
                {
                    "call_id": str(ctx.call_id),
                    "contact_id": contact_id,
                    "outcome": outcome.outcome,
                    "sip_status": outcome.sip_status,
                    "ring_ms": outcome.ring_ms,
                },
            )
        except Exception:
            _log.exception("handover result post failed")
        return _CONNECTED if outcome.outcome == "answered" else _NO_ANSWER

    return dataclasses.replace(
        SPEC,
        description=(
            "Connect the caller to a person on the team when they ask for a "
            "person or when the note says so. First tell the caller you are "
            f"connecting them. People: {menu}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "enum": list(by_label)},
                "reason": {
                    "type": "string",
                    "description": "One short phrase for the person, e.g. 'an invoice question'.",
                },
            },
            "required": ["contact", "reason"],
        },
        execute=execute,
        bind=None,
    )


SPEC = dataclasses.replace(SPEC, bind=bind)

__all__ = ["MAX_REASON_CHARS", "SPEC", "bind"]
