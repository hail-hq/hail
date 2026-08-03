"""send_email — email an org member from the directory. outbound_send tier.

Recipients are directory names only (see ``list_contacts``); dictated
addresses are unverifiable over voice, so raw addresses are never
accepted. Resolution, gate, cap, disclosure footer, and billing run
server-side in ``/internal/agent/send-email``.
"""

from __future__ import annotations

import uuid
from typing import Any

from hailhq.core.agent_tools.spec import SPOKEN_FALLBACK, ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.models import EmailDomain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The internal route (AgentSendEmailRequest) imports these as its Field caps.
MAX_RECIPIENT_NAME_CHARS = 200
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
    if (await session.execute(stmt)).first() is not None:
        return True
    # No verified domain yet doesn't mean unavailable: routes/emails.py
    # resolve_sender() falls back to auto-minting a hail-mail EmailDomain
    # on the tenant's first send. Mirror that mint branch's requirements
    # exactly (resolve_hail_mail_prefixes / compose_hail_mail_address in
    # routes/email_domains.py) rather than just checking for existing rows:
    # HAIL_MAIL_BASE_DOMAIN must be configured, and a user prefix must be
    # resolvable from HAIL_MAIL_FROM or HAIL_MAIL_DEFAULT_USER_PREFIX (the
    # org prefix is always derivable from organization_id, so it never
    # blocks the mint).
    return bool(settings.hail_mail_base_domain) and bool(
        settings.hail_mail_from or settings.hail_mail_default_user_prefix
    )


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.api is None:
        return _UNAVAILABLE
    recipient_name = str(args.get("recipient_name", "")).strip()[
        :MAX_RECIPIENT_NAME_CHARS
    ]
    subject = str(args.get("subject", ""))[:MAX_SUBJECT_CHARS]
    body_text = str(args.get("body_text", ""))[:MAX_BODY_CHARS]
    # Raw LiveKit tool calls aren't schema-validated the way the internal
    # route's Pydantic model is (min_length=1) — an empty string would sail
    # through and 422 at the route, surfacing only a generic apology. Catch
    # it here with tailored, actionable prompts instead of posting at all.
    if not recipient_name:
        return "I need the recipient's name before I can send the email."
    if not subject.strip() or not body_text.strip():
        return "I need a subject and a message before I can send the email."
    resp = await ctx.api.post(
        "/internal/agent/send-email",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": recipient_name,
            "subject": subject,
            "body_text": body_text,
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


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
