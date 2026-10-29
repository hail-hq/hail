"""transfer_call — hand the live call to a person the admin picked.

session_control tier: the agent finishes its sentence first. The LLM only
sees names and notes (dispatch metadata ``handover_targets``); the API
resolves and re-checks the number (``/internal/agent/handover``), the
voicebot's ``bridge`` handle dials it into the room, and the API records
the outcome (``/internal/agent/handover-result``). While it dials, the API
texts and emails the person the caller's number and the reason
(``/internal/agent/handover-sms``, ``/internal/agent/handover-email``).

The dial runs in the background: the tool returns at once, so the agent
keeps talking with the caller while it rings, and ``cancel_transfer``
stops the ring. When the dial ends without a connect, ``ctx.prompt`` has
the agent tell the caller.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import uuid
from collections.abc import Awaitable
from typing import Any

from hailhq.core.agent_tools.spec import (
    SPOKEN_FALLBACK,
    BridgeOutcome,
    BridgeRoute,
    ToolContext,
    ToolSpec,
)
from hailhq.core.handover import ON_CALL_LINE
from sqlalchemy.ext.asyncio import AsyncSession

MAX_REASON_CHARS = 200

_log = logging.getLogger("hailhq.core.agent_tools")

_UNAVAILABLE = "I can't connect you to anyone right now."
# Instruction to the agent when the dial ends without a connect.
_NO_ANSWER = "{name} could not pick up. Tell the caller and offer to take a message."
# The tool's answer while another dial still rings.
_STILL_RINGING = (
    "{name} is still ringing. To try someone else, call cancel_transfer first."
)
# How long a result post waits for the text and email to finish, so it
# records what became of them rather than "pending".
_NOTICE_WAIT_SECONDS = 5.0
# Waits between the 7 attempts of the "answered" result post (about a minute
# in all). The stale-call and pool sweeps rely on that event to lift the time
# limit, so a short API outage must not lose it.
_ANSWERED_BACKOFF = (0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


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
    uninterruptible=True,
)


# Text, email and dial tasks still running when the tool returns: held so
# they are not garbage collected before they finish.
_pending: set[asyncio.Task] = set()


def _hold(task: asyncio.Task) -> asyncio.Task:
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


async def _notify_contact(ctx: ToolContext, channel: str, body: dict[str, Any]) -> str:
    """Ask the API to text (``sms``) or email (``email``) the contact; what
    became of it, never raises."""
    try:
        reply = await ctx.api.post(f"/internal/agent/handover-{channel}", body)
    except Exception:
        _log.exception("handover %s post failed", channel)
        return "failed"
    return str(reply.get(channel) or "failed")


def _notice_state(task: asyncio.Task[str]) -> str:
    if not task.done():
        return "pending"
    return "failed" if task.cancelled() else task.result()


async def _post_answered(ctx: ToolContext, body: dict[str, Any]) -> None:
    """Post the "answered" result; retried, never raises."""
    for attempt in range(len(_ANSWERED_BACKOFF) + 1):
        try:
            await ctx.api.post("/internal/agent/handover-result", body)
            return
        except Exception:
            _log.exception("handover answered post failed")
        if attempt < len(_ANSWERED_BACKOFF):
            await asyncio.sleep(_ANSWERED_BACKOFF[attempt])


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
    # The other party on this call: listed so the agent can explain, never
    # dialed (it would ring the phone already in use).
    on_call = {t["label"] for t in targets if t.get("on_call")}

    def _entry(t: dict) -> str:
        if t["label"] in on_call:
            line = ON_CALL_LINE.format(name=t["label"])
            return f"{t['label']} (already on this call, do not connect; say: {line})"
        return f"{t['label']} ({t.get('note') or ''})"

    menu = "; ".join(_entry(t) for t in targets)
    # This call's dial in progress, if any: (label, task).
    ringing: list[tuple[str, asyncio.Task]] = []

    async def execute(ctx: ToolContext, args: dict[str, Any]) -> str | None:
        if ctx.api is None or ctx.bridge is None:
            return _UNAVAILABLE
        label = str(args.get("contact", ""))
        contact_id = by_label.get(label)
        if contact_id is None:
            return "I can only connect you to: " + ", ".join(by_label) + "."
        if label in on_call:
            return ON_CALL_LINE.format(name=label)
        if ringing and not ringing[0][1].done():
            return _STILL_RINGING.format(name=ringing[0][0])
        reason = " ".join(str(args.get("reason", "")).split())[:MAX_REASON_CHARS]

        # One id per attempt: the route stores one event per id, so a retried
        # post (timeout, dropped connection) is not recorded twice.
        invocation_id = str(uuid.uuid4())

        sms: asyncio.Task[str] | None = None
        email: asyncio.Task[str] | None = None

        async def _result(outcome: str, sip_status: int | None, ring_ms: int) -> dict:
            notices = {t for t in (sms, email) if t is not None and not t.done()}
            if notices:
                await asyncio.wait(notices, timeout=_NOTICE_WAIT_SECONDS)
            return {
                "tool_invocation_id": invocation_id,
                "call_id": str(ctx.call_id),
                "contact_id": contact_id,
                "outcome": outcome,
                "sip_status": sip_status,
                "ring_ms": ring_ms,
                "sms": _notice_state(sms) if sms is not None else None,
                "email": _notice_state(email) if email is not None else None,
            }

        answer_reported = False

        async def _answered(ring_ms: int) -> None:
            await _post_answered(ctx, await _result("answered", None, ring_ms))

        def on_answered(ring_ms: int) -> Awaitable[None]:
            # Called once someone on the contact's side speaks; the bridge
            # runs the returned post in the background. The flag is set
            # here, synchronously, so the result is never posted twice.
            nonlocal answer_reported
            answer_reported = True
            return _answered(ring_ms)

        route = await ctx.api.post(
            "/internal/agent/handover",
            {"call_id": str(ctx.call_id), "contact_id": contact_id},
        )
        if not route.get("ok"):
            return str(route.get("spoken") or SPOKEN_FALLBACK)
        notice = {
            "tool_invocation_id": invocation_id,
            "call_id": str(ctx.call_id),
            "contact_id": contact_id,
            "reason": reason,
        }
        sms = _hold(asyncio.create_task(_notify_contact(ctx, "sms", notice)))
        email = _hold(asyncio.create_task(_notify_contact(ctx, "email", notice)))
        bridge = ctx.bridge

        async def _dial() -> None:
            try:
                outcome = await bridge(
                    BridgeRoute(
                        to_e164=route["to_e164"],
                        from_e164=route["from_e164"],
                        trunk_id=route["trunk_id"],
                        headers=route.get("headers"),
                        name=label,
                        on_answered=on_answered,
                    )
                )
            except Exception:
                _log.exception("handover bridge failed")
                outcome = BridgeOutcome("failed", None, 0)
            if outcome.outcome == "answered" and answer_reported:
                return
            try:
                await ctx.api.post(
                    "/internal/agent/handover-result",
                    await _result(outcome.outcome, outcome.sip_status, outcome.ring_ms),
                )
            except Exception:
                _log.exception("handover result post failed")
            if outcome.outcome in ("answered", "cancelled") or ctx.prompt is None:
                # Connected: the agent is muted. Cancelled: cancel_transfer
                # already gave the agent its turn.
                return
            try:
                await ctx.prompt(_NO_ANSWER.format(name=label))
            except Exception:
                _log.exception("handover no-answer prompt failed")

        ringing[:] = [(label, _hold(asyncio.create_task(_dial())))]
        # None: no reply turn. The agent already told the caller it is
        # connecting them and that it is still here while it rings.
        return None

    return dataclasses.replace(
        SPEC,
        description=(
            "Connect the caller to a person on the team when they ask for a "
            "person or when the note says so. First tell the caller you are "
            "connecting them, and that you are still here while it rings so "
            "they can say stop if they change their mind. This tool starts "
            "the ring and returns at once; say nothing more unless the "
            f"caller speaks. People: {menu}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "contact": {"type": "string", "enum": list(by_label)},
                "reason": {
                    "type": "string",
                    "description": (
                        "What the caller needs, as a short phrase the person "
                        "reads in a text and email, e.g. 'an invoice question'. "
                        "Leave it out when the caller only asked for a person."
                    ),
                },
            },
            "required": ["contact"],
        },
        execute=execute,
        bind=None,
    )


SPEC = dataclasses.replace(SPEC, bind=bind)


async def _cancel(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.cancel_bridge is None:
        return _UNAVAILABLE
    if await ctx.cancel_bridge():
        return "The ring is stopped. Ask the caller what they would like instead."
    return "Nothing is ringing, so there is nothing to stop."


# Bound next to transfer_call (never on its own, see voicebot tools.py).
CANCEL_SPEC = ToolSpec(
    name="cancel_transfer",
    description=(
        "Stop ringing the person you are connecting the caller to. Use it "
        "when the caller asks to stop, changes their mind, or wants someone "
        "else while it still rings."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    risk_tier="session_control",
    is_available=_always,
    execute=_cancel,
)

__all__ = ["CANCEL_SPEC", "MAX_REASON_CHARS", "SPEC", "bind"]
