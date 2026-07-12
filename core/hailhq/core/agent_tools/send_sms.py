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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber

MAX_BODY_CHARS = 480  # ~3 SMS segments; matches the internal route's cap

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
    if ctx.api is None:
        return _UNAVAILABLE
    resp = await ctx.api.post(
        "/internal/agent/send-sms",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": str(args.get("body", ""))[:MAX_BODY_CHARS],
        },
    )
    return str(resp.get("spoken", "Sorry, that didn't work."))


SPEC = ToolSpec(
    name="send_sms",
    description=(
        "Send a text message to the person on this call, at the number being "
        "called. You cannot text anyone else. Before sending, say exactly "
        "what the message will say and get their confirmation."
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
