"""send_sms — text the person on this call. outbound_send tier.

The only possible recipient is the call counterpart: org members carry
no phone numbers (the better-auth users table has none), and raw numbers
are never accepted as parameters. Recipient resolution, the compliance
gate, the per-call cap, and billing all run server-side in
``/internal/agent/send-sms``.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.spec import NO_CALL, SPOKEN_FALLBACK, ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

MAX_BODY_CHARS = 480  # ~3 SMS segments; the internal route imports this as its cap
MAX_RECIPIENT_NAME_CHARS = 200

_UNAVAILABLE = "Text messaging is not available right now."


async def _is_available(organization_id: uuid.UUID, session: AsyncSession) -> bool:
    if not settings.hail_internal_secret:
        return False
    stmt = (
        select(PhoneNumber.id)
        .where(
            PhoneNumber.organization_id == organization_id,
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.capabilities.any("sms"),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.call_id is None:
        return NO_CALL
    if ctx.api is None:
        return _UNAVAILABLE
    body_text = str(args.get("body", ""))[:MAX_BODY_CHARS]
    # Raw LiveKit tool calls aren't schema-validated the way the internal
    # route's Pydantic model is (min_length=1) — an empty string would sail
    # through and 422 at the route, surfacing only a generic apology. Catch
    # it here with a tailored, actionable prompt instead of posting at all.
    if not body_text.strip():
        return "I need the message text before I can send it."
    resp = await ctx.api.post(
        "/internal/agent/send-sms",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": body_text,
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


async def _execute_for_email(ctx: ToolContext, args: dict[str, Any]) -> str:
    """An email run has no one on a call: the recipient is a directory name,
    and the route resolves the number. The model never sees or supplies one."""
    if ctx.inbound_email_id is None:
        return _UNAVAILABLE
    if ctx.api is None:
        return _UNAVAILABLE
    recipient_name = str(args.get("recipient_name", "")).strip()[
        :MAX_RECIPIENT_NAME_CHARS
    ]
    body_text = str(args.get("body", ""))[:MAX_BODY_CHARS]
    if not recipient_name:
        return "I need the recipient's name before I can send the text."
    if not body_text.strip():
        return "I need the message text before I can send it."
    resp = await ctx.api.post(
        "/internal/agent/email-run/send-sms",
        {
            "inbound_email_id": str(ctx.inbound_email_id),
            # Same message, same id: a retried run cannot send it twice.
            "tool_invocation_id": str(
                uuid.uuid5(
                    ctx.inbound_email_id,
                    f"send_sms\x00{recipient_name}\x00{body_text}",
                )
            ),
            "recipient_name": recipient_name,
            "body": body_text,
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


SPEC = ToolSpec(
    name="send_sms",
    description=(
        "Send a text message to the person on this call, at the number being "
        "called. You cannot text anyone else. When they ask for the text, "
        "send it right away; their request is the confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "The text message to send. Keep it short.",
                "maxLength": MAX_BODY_CHARS,
            }
        },
        "required": ["body"],
    },
    risk_tier="outbound_send",
    is_available=_is_available,
    execute=_execute,
)

# In an email run the recipient is a member named in the directory.
EMAIL_RUN_SPEC = ToolSpec(
    name="send_sms",
    description=(
        "Send a text message to a member of the organization's team directory "
        "(use list_contacts to see who). You cannot text arbitrary numbers, "
        "only directory names."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient_name": {
                "type": "string",
                "description": "The directory name of the recipient.",
            },
            "body": {
                "type": "string",
                "description": "The text message to send. Keep it short.",
                "maxLength": MAX_BODY_CHARS,
            },
        },
        "required": ["recipient_name", "body"],
    },
    risk_tier="outbound_send",
    is_available=_is_available,
    execute=_execute_for_email,
)
