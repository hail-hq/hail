"""send_email — email an org member from the directory. outbound_send tier.

Recipients are directory names only (see ``list_contacts``); dictated
addresses are unverifiable over voice, so raw addresses are never
accepted. Resolution, gate, cap, disclosure footer, and billing run
server-side in ``/internal/agent/send-email``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import EmailDomain

MAX_RECIPIENT_NAME_CHARS = 200  # route schema (AgentSendEmailRequest) max
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 5000

_UNAVAILABLE = "Email is not available right now."


async def _is_available(organization_id: uuid.UUID, session: AsyncSession) -> bool:
    if not settings.hail_internal_secret:
        return False
    stmt = (
        select(EmailDomain.id)
        .where(
            EmailDomain.organization_id == organization_id,
            EmailDomain.verification_status == "verified",
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.api is None:
        return _UNAVAILABLE
    resp = await ctx.api.post(
        "/internal/agent/send-email",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": str(args.get("recipient_name", "")).strip()[
                :MAX_RECIPIENT_NAME_CHARS
            ],
            "subject": str(args.get("subject", ""))[:MAX_SUBJECT_CHARS],
            "body_text": str(args.get("body_text", ""))[:MAX_BODY_CHARS],
        },
    )
    return str(resp.get("spoken", "Sorry, that didn't work."))


SPEC = ToolSpec(
    name="send_email",
    description=(
        "Send an email to a member of the organization's team directory "
        "(use list_contacts to see who). You cannot email arbitrary "
        "addresses — only directory names. Before sending, say the "
        "recipient's name and summarize the content, and get confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "recipient_name": {
                "type": "string",
                "description": "The directory name of the recipient.",
            },
            "subject": {"type": "string", "maxLength": MAX_SUBJECT_CHARS},
            "body_text": {
                "type": "string",
                "description": "Plain-text email body.",
                "maxLength": MAX_BODY_CHARS,
            },
        },
        "required": ["recipient_name", "subject", "body_text"],
    },
    risk_tier="outbound_send",
    is_available=_is_available,
    execute=_execute,
)
