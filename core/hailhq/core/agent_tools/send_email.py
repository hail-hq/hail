"""send_email — email a member or saved contact by name. outbound_send tier.

Recipients are names of org members (see ``list_contacts``) or saved
contacts with an email address, or :data:`SELF_RECIPIENT` for the person
on the call or the sender of the email. A spoken or typed address is never
accepted: nothing proves the person owns it, and it would let anyone use
the org's domain to send spam. Resolution, gate, cap, and billing run server-side in
``/internal/agent/send-email``.
"""

from __future__ import annotations

import dataclasses
import json
import unicodedata
import uuid
from typing import Any

from hailhq.core.agent_tools.spec import NO_CALL, SPOKEN_FALLBACK, ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.email_sender import head_retired_by_other_org
from hailhq.core.hail_mail import default_user_prefix
from hailhq.core.models import EmailDomain
from hailhq.core.schemas import RESERVED_USER_PREFIXES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The internal route (AgentSendEmailRequest) imports these as its Field caps.
MAX_RECIPIENT_NAME_CHARS = 200
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 5000

# recipient_name for the person the agent talks to: the caller's own address
# on file on a call, the proven sender in an email run. Never a name lookup.
SELF_RECIPIENT = "me"
# "sender" and "you" are older spellings an email run already used.
_SELF_NAMES = frozenset({SELF_RECIPIENT, "caller", "sender", "you"})


def is_self_recipient(name: str) -> bool:
    """True when ``recipient_name`` means the person the agent talks to."""
    return name.strip().casefold() in _SELF_NAMES


_UNAVAILABLE = "Email is not available right now."
# Longest org name in the fixed lines of an email to the caller.
MAX_ORG_NAME_CHARS = 100
# Control, format and surrogate characters: they could break or disguise text.
_HIDDEN = frozenset({"Cc", "Cf", "Cs"})


def _one_line(text: str) -> str:
    """``text`` on one line, without characters that could disguise it."""
    kept = "".join(
        " " if c.isspace() else c
        for c in text
        if c.isspace() or unicodedata.category(c) not in _HIDDEN
    )
    return " ".join(kept.split())


def _call_with(org_name: str | None) -> str:
    name = _one_line(org_name or "")[:MAX_ORG_NAME_CHARS].strip()
    return f"a phone call with {name}" if name else "a phone call"


def caller_email_subject(subject: str, org_name: str | None) -> str:
    """The subject of an email to the caller's own address on file: fixed
    words first, so the email list shows why it came before any words the
    caller chose."""
    return f"From {_call_with(org_name)}: {_one_line(subject)}"


def caller_email_body(body_text: str, org_name: str | None) -> str:
    """The body of an email to the caller's own address on file: a fixed
    first line and footer around the agent's text. Caller ID picked the
    address and can be faked, so the reader learns why it came and that
    they can ignore it."""
    call = _call_with(org_name)
    return (
        f"This email was sent because of {call}.\n\n"
        f"{body_text.strip()}\n\n"
        "-- \n"
        f"You got this email because someone asked for it during {call},"
        " from a phone number saved with this email address. If that was not"
        " you, you can ignore it."
    )


async def _is_available(organization_id: uuid.UUID, session: AsyncSession) -> bool:
    if not settings.hail_internal_secret:
        return False
    stmt = (
        select(EmailDomain.id)
        .where(
            EmailDomain.organization_id == organization_id,
            EmailDomain.verification_status == "verified",
            # A switched-off custom domain cannot send (verified_senders
            # skips it), so it does not make email available.
            EmailDomain.sends_enabled,
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
    # blocks the mint). The mint also refuses a role name, and a head another
    # org retired.
    if not settings.hail_mail_base_domain:
        return False
    pinned = settings.hail_mail_from.partition("@")[0].partition("+")[0]
    default = settings.hail_mail_default_user_prefix
    if not pinned and not default:
        return False
    head = pinned or default_user_prefix(default, organization_id)
    if head in RESERVED_USER_PREFIXES:
        return False
    return not await head_retired_by_other_org(session, head, organization_id)


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.call_id is None and ctx.inbound_email_id is None:
        return NO_CALL
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
    if ctx.inbound_email_id is not None:
        # Same message, same id: a retried run cannot send it twice, and a
        # different message to the same person still goes.
        files = (
            json.dumps(
                {
                    key: args[key]
                    for key in ("reply_to_email_id", "attachment_ids")
                    if args.get(key)
                },
                sort_keys=True,
            )
            if args.get("reply_to_email_id") or args.get("attachment_ids")
            else ""
        )
        invocation = uuid.uuid5(
            ctx.inbound_email_id,
            f"send_email\x00{recipient_name}\x00{subject}\x00{body_text}" + files,
        )
        path = "/internal/agent/email-run/send-email"
        source = {"inbound_email_id": str(ctx.inbound_email_id)}
    else:
        invocation = uuid.uuid4()
        path = "/internal/agent/send-email"
        source = {"call_id": str(ctx.call_id)}
    resp = await ctx.api.post(
        path,
        {
            **source,
            "tool_invocation_id": str(invocation),
            "recipient_name": recipient_name,
            "subject": subject,
            "body_text": body_text,
            **{
                key: args[key]
                for key in ("reply_to_email_id", "attachment_ids")
                if args.get(key)
            },
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


SPEC = ToolSpec(
    name="send_email",
    description=(
        "Send an email to a team member (use list_contacts to see who) or a "
        "saved contact, by name. When the person on the call wants it sent to "
        f'themselves ("me", "my email"), set recipient_name to "{SELF_RECIPIENT}". '
        "It cannot email an address someone says or "
        "types. If asked to, politely say you can only email addresses already "
        "on file, to prevent spam. When the person on the call asks "
        "for the email, send it right away; their request is the "
        "confirmation. Reattach original files with their attachment_ids and reply_to_email_id from this person’s email thread. Never invent file IDs. Then say it is sent. Ask one short question only when "
        "the recipient, the channel (text or email) or the content is "
        "unclear, and never ask the same question twice. Before sending "
        "something they did not ask for, say what you will send and to whom, "
        "and wait for a yes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "reply_to_email_id": {
                "type": "string",
                "format": "uuid",
                "description": "The email to reply to, from the conversation inbox tool.",
            },
            "attachment_ids": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string", "format": "uuid"},
                "description": "Original file IDs from that email thread. Requires reply_to_email_id. Never invent IDs.",
            },
            "recipient_name": {
                "type": "string",
                "description": (
                    "The name of a team member or saved contact, or "
                    f'"{SELF_RECIPIENT}" for the person you are talking to.'
                ),
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

# In an email run there is no one on a call. The rest of the spec is the same.
EMAIL_RUN_SPEC = dataclasses.replace(
    SPEC,
    description=(
        "Send an email to a team member (use list_contacts to see who) or a "
        "saved contact, by name. To email the sender of this email, set "
        f'recipient_name to "{SELF_RECIPIENT}": that email is your reply, in this thread, so do not reply again. You cannot email an address someone writes: '
        "you can only email addresses already on file, to prevent spam. To reply with original files to the authenticated sender, use recipient_name=sender, attachment_ids from read_email, and the current reply_to_email_id. This sends the final reply with those original files; do not send it twice."
    ),
)
