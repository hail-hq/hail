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
from hailhq.core.caller_email_lines import caller_email_lines
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
_CALL_SELF_NAMES = frozenset({SELF_RECIPIENT, "caller"})
# "sender" and "you" are older spellings an email run already used. On a call
# "you" may mean the business, so it is a name there.
_RUN_SELF_NAMES = _CALL_SELF_NAMES | {"sender", "you"}


def is_self_recipient(name: str, *, on_call: bool = False) -> bool:
    """True when ``recipient_name`` means the person the agent talks to."""
    names = _CALL_SELF_NAMES if on_call else _RUN_SELF_NAMES
    return name.strip().casefold() in names


_UNAVAILABLE = "Email is not available right now."
# Longest org name in the fixed lines of an email to the caller.
MAX_ORG_NAME_CHARS = 100
# Control, format and surrogate characters: they could break or disguise text.
_HIDDEN = frozenset({"Cc", "Cf", "Cs"})
# Combining marks kept on one letter: more would pile over nearby text.
# Fully pointed Hebrew uses up to six.
_MAX_MARKS = 8
# Thai and Lao vowels written before their consonant. At the end of an org
# name they have no consonant and would join the fixed words after it.
_LEADING_VOWELS = frozenset(
    "\u0e40\u0e41\u0e42\u0e43\u0e44\u0ec0\u0ec1\u0ec2\u0ec3\u0ec4"
)


def _one_line(text: str) -> str:
    """``text`` on one line, in NFC, without characters that could disguise
    it. Joiners are format characters, so they go. A combining mark with no
    letter before it, or past eight on one letter, goes too: it would draw
    over the fixed words."""
    kept: list[str] = []
    marks = 0
    for c in unicodedata.normalize("NFC", text):
        category = unicodedata.category(c)
        if c.isspace():
            kept.append(" ")
            marks = 0
        elif category in _HIDDEN:
            continue
        elif category[0] == "M":
            if not kept or kept[-1] == " " or marks >= _MAX_MARKS:
                continue
            kept.append(c)
            marks += 1
        else:
            kept.append(c)
            marks = 0
    return " ".join("".join(kept).split())


def _org_name(org_name: str | None, limit: int = MAX_ORG_NAME_CHARS) -> str:
    name = _one_line(org_name or "")[:limit].strip()
    while name and name[-1] in _LEADING_VOWELS:
        name = name[:-1].rstrip()
    return name


def _fill(named: str, unnamed: str, name: str) -> str:
    """``named`` with the org name, or ``unnamed`` without one. When a letter
    follows ``{org}`` directly (Telugu ``{org}తో``) and the name ends in a
    virama, a zero width non-joiner keeps the name from joining the fixed
    word into one conjunct. The name keeps its letters (after the NFC
    cleanup in :func:`_one_line`)."""
    if not name:
        return unnamed
    before, _, after = named.partition("{org}")
    joins = after[:1] and unicodedata.category(after[0])[0] in "LM"
    if joins and unicodedata.combining(name[-1]) == 9:  # virama
        name += "\u200c"
    return f"{before}{name}{after}"


def caller_email_subject(
    subject: str, org_name: str | None, language: str | None = None
) -> str:
    """The subject of an email to the caller's own address on file: fixed
    words first, in the call's language, so the email list shows why it came
    before any words the caller chose. A long org name is cut so the fixed
    words always fit; the caller's words are cut last."""
    lines = caller_email_lines(language)
    fixed = len(lines.subject.replace("{org}", "")) + 2  # ": "
    name = _org_name(org_name, min(MAX_ORG_NAME_CHARS, MAX_SUBJECT_CHARS - fixed))
    prefix = _fill(lines.subject, lines.subject_unnamed, name)
    words = _one_line(subject)
    framed = f"{prefix}: {words}" if words else prefix
    return framed[:MAX_SUBJECT_CHARS]


def caller_email_body(
    body_text: str, org_name: str | None, language: str | None = None
) -> str:
    """The body of an email to the caller's own address on file: a fixed
    first line and footer, in the call's language, around the agent's text.
    Caller ID picked the address and can be faked, so the reader learns why
    it came and that they can ignore it."""
    lines = caller_email_lines(language)
    name = _org_name(org_name)
    return (
        f"{_fill(lines.first_line, lines.first_line_unnamed, name)}\n\n"
        f"{body_text.strip()}\n\n"
        "-- \n"
        f"{_fill(lines.footer, lines.footer_unnamed, name)}"
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
        # Every self word reaches the sender: one key, so a retried run that
        # picks another word does not send twice.
        key_name = (
            SELF_RECIPIENT if is_self_recipient(recipient_name) else recipient_name
        )
        invocation = uuid.uuid5(
            ctx.inbound_email_id,
            f"send_email\x00{key_name}\x00{subject}\x00{body_text}" + files,
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
        f'recipient_name to "{SELF_RECIPIENT}", only for something separate from your reply: your normal reply still goes to them too. You cannot email an address someone writes: '
        f'you can only email addresses already on file, to prevent spam. To reply with original files to the sender, use recipient_name "{SELF_RECIPIENT}", attachment_ids from read_email, and the current reply_to_email_id. This sends the final reply with those original files; do not send it twice.'
    ),
)
