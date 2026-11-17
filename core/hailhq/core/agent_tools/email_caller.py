"""email_caller — email the person on this call. outbound_send tier.

Opt-in per agent (``agents.email_caller_enabled``), so it is not in
``all_tools()``: the voicebot adds it when the dispatch metadata turns it on.
The caller dictates the address; the agent reads it back and sends only
after a yes. Plain text with links, one recipient, no attachments. The
switch, the per-call cap, the gate and billing run server-side in
``/internal/agent/email-caller``.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.send_email import MAX_SUBJECT_CHARS
from hailhq.core.agent_tools.send_email import _is_available as _email_available
from hailhq.core.agent_tools.spec import NO_CALL, SPOKEN_FALLBACK, ToolContext, ToolSpec
from hailhq.core.schemas import EMAIL_ADDR

# The internal route (AgentEmailCallerRequest) imports these as its caps.
MAX_ADDRESS_CHARS = 254
MAX_BODY_CHARS = 2000
# Emails to the person on one call. Each goes to the same address.
MAX_EMAILS_PER_CALL = 2

# The dispatch metadata key that turns the tool on for a call.
METADATA_KEY = "email_caller_enabled"
# The call_events kind the API writes for each email it sends.
EVENT_KIND = "email_caller"

_UNAVAILABLE = "Email is not available right now."


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.call_id is None or ctx.inbound_email_id is not None:
        return NO_CALL
    if ctx.api is None:
        return _UNAVAILABLE
    to_address = str(args.get("to_address", "")).strip()[:MAX_ADDRESS_CHARS]
    subject = str(args.get("subject", "")).strip()[:MAX_SUBJECT_CHARS]
    body_text = str(args.get("body_text", ""))[:MAX_BODY_CHARS]
    # Raw LiveKit tool calls are not schema-validated: catch what the route
    # would reject with a 422, and say what is missing instead.
    if not EMAIL_ADDR.match(to_address):
        return "I need a full email address before I can send the email."
    if not subject or not body_text.strip():
        return "I need a subject and a message before I can send the email."
    resp = await ctx.api.post(
        "/internal/agent/email-caller",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": to_address,
            "subject": subject,
            "body_text": body_text,
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


SPEC = ToolSpec(
    name="email_caller",
    description=(
        "Email the person on this call something they asked for, such as a "
        "link or a short follow-up, at an address they give you. Before you "
        "send, read the address back letter by letter where it is unclear, "
        "and send only after they say yes. Never guess or complete an "
        "address. Plain text and links only; you cannot attach files. One "
        "address per call, and at most "
        f"{MAX_EMAILS_PER_CALL} emails per call. Then say it is sent."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to_address": {
                "type": "string",
                "description": "The address the person gave and confirmed.",
                "maxLength": MAX_ADDRESS_CHARS,
            },
            "subject": {"type": "string", "maxLength": MAX_SUBJECT_CHARS},
            "body_text": {
                "type": "string",
                "description": "Plain-text email body. Links are fine.",
                "maxLength": MAX_BODY_CHARS,
            },
        },
        "required": ["to_address", "subject", "body_text"],
    },
    risk_tier="outbound_send",
    is_available=_email_available,
    execute=_execute,
)


__all__ = [
    "EVENT_KIND",
    "MAX_ADDRESS_CHARS",
    "MAX_BODY_CHARS",
    "MAX_EMAILS_PER_CALL",
    "METADATA_KEY",
    "SPEC",
]
