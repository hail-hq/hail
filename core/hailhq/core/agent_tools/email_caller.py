"""email_caller — email the person on this call. outbound_send tier.

Opt-in per agent (``agents.email_caller_enabled``), so it is not in
``all_tools()``: the voicebot adds it when the dispatch metadata turns it on,
whatever the ``tools`` list says, on a phone call with a known number.
The caller dictates the address; the agent reads it back and sends only
after a yes. Plain text with links, one recipient, no attachments. The
switch, the caps, the gate and billing run server-side in
``/internal/agent/email-caller``.

Relay risk: anyone who can call the number can ask for an email to any
address, with text they dictate. The caps below bound that, and
``framed_body`` wraps every email in fixed lines the agent cannot change.
"""

from __future__ import annotations

import dataclasses
import functools
import re
import unicodedata
import uuid
from typing import Any

from hailhq.core.agent_tools.send_email import MAX_SUBJECT_CHARS
from hailhq.core.agent_tools.send_email import _is_available as _email_available
from hailhq.core.agent_tools.spec import NO_CALL, SPOKEN_FALLBACK, ToolContext, ToolSpec

# The internal route (AgentEmailCallerRequest) imports these as its caps.
MAX_ADDRESS_CHARS = 254
MAX_BODY_CHARS = 2000
# Emails to the person on one call, all to one address.
MAX_EMAILS_PER_CALL = 2
# Per caller phone number, over any 24 hours, across all calls.
MAX_EMAILS_PER_CALLER_PER_DAY = 3
MAX_ADDRESSES_PER_CALLER_PER_DAY = 2
# Per organization, over any 24 hours.
MAX_EMAILS_PER_ORG_PER_DAY = 100
# Longest organization name shown in the fixed lines.
MAX_ORG_NAME_CHARS = 80

# One plain mailbox, ASCII only: no quotes, brackets, commas, spaces or
# invisible characters that make two addresses look the same. The route
# uses the same pattern.
ADDRESS_PATTERN = r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$"
_ADDRESS = re.compile(ADDRESS_PATTERN)

# The dispatch metadata key that turns the tool on for a call.
METADATA_KEY = "email_caller_enabled"
# The call_events kind the API writes for each email it sends.
EVENT_KIND = "email_caller"

_UNAVAILABLE = "Email is not available right now."


def _call_with(org_name: str | None) -> str:
    # Same cleaning as the subject: no control or format characters in the
    # fixed words.
    name = clean_subject(org_name or "")[:MAX_ORG_NAME_CHARS]
    return f"a phone call with {name}" if name else "a phone call"


def clean_subject(subject: str) -> str:
    """The dictated subject on one line, with control and format characters
    (newlines, bidi overrides, zero-width marks) removed: in a Subject header
    they break it, get the send refused, or disguise the text. May be empty."""
    kept = "".join(
        " " if c.isspace() else c
        for c in subject
        if c.isspace() or not unicodedata.category(c).startswith("C")
    )
    return " ".join(kept.split())


def framed_subject(subject: str, org_name: str | None) -> str:
    """The subject as sent: fixed words first, so the inbox list shows that
    the caller asked for it before any dictated words."""
    return f"From {_call_with(org_name)}: {clean_subject(subject)}"


def clean_body(body_text: str) -> str:
    """The dictated body without control and format characters (bidi
    overrides, zero-width marks) that could disguise its text. Newlines and
    tabs stay."""
    return "".join(
        c
        for c in body_text
        if c in "\n\t" or not unicodedata.category(c).startswith("C")
    ).strip()


def framed_body(body_text: str, org_name: str | None) -> str:
    """The email as sent: a fixed first line and footer around the agent's
    text, so a dictated message cannot pass as the organization's own mail
    and the reader knows why it came."""
    call = _call_with(org_name)
    return (
        f"You asked for this during {call}.\n\n"
        f"{clean_body(body_text)}\n\n"
        "--\n"
        f"You got this email because this address was given during {call}. "
        "If that was not you, you can ignore it."
    )


async def _execute(
    ctx: ToolContext, args: dict[str, Any], *, org_name: str | None = None
) -> str:
    if ctx.call_id is None or ctx.inbound_email_id is not None:
        return NO_CALL
    if ctx.api is None:
        return _UNAVAILABLE
    to_address = str(args.get("to_address", "")).strip()
    subject = clean_subject(str(args.get("subject", "")))
    body_text = str(args.get("body_text", ""))
    # Raw LiveKit tool calls are not schema-validated: catch what the route
    # would reject with a 422, and say what to fix. Never cut: a cut address
    # is another address, and a cut body can end inside a link.
    if len(to_address) > MAX_ADDRESS_CHARS or not _ADDRESS.match(to_address):
        return "I need a full email address before I can send the email."
    if not subject or not clean_body(body_text):
        return "I need a subject and a message before I can send the email."
    if len(subject) > MAX_SUBJECT_CHARS:
        return f"The subject is too long. Keep it under {MAX_SUBJECT_CHARS} characters. Nothing was sent."
    if len(body_text) > MAX_BODY_CHARS:
        return f"The message is too long. Keep it under {MAX_BODY_CHARS} characters. Nothing was sent."
    resp = await ctx.api.post(
        "/internal/agent/email-caller",
        {
            "call_id": str(ctx.call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": to_address,
            "subject": subject,
            "body_text": body_text,
            # From the dispatch, so the API does not look it up under its
            # locks. The API falls back to its own copy when it is missing.
            **({"org_name": org_name} if org_name else {}),
        },
    )
    return str(resp.get("spoken", SPOKEN_FALLBACK))


def _bind(metadata: dict[str, Any]) -> ToolSpec:
    """This call's tool: carries the organization name from the dispatch."""
    name = metadata.get("org_name")
    # Blank or not text: send none, so the API reads its own copy.
    # _call_with cleans and cuts it.
    name = name if isinstance(name, str) and name.strip() else None
    return dataclasses.replace(
        SPEC, execute=functools.partial(_execute, org_name=name), bind=None
    )


SPEC = ToolSpec(
    name="email_caller",
    description=(
        "Email the person on this call something they asked for, such as a "
        "link or a short follow-up, at an address they give you. Before you "
        "send, read the address back letter by letter where it is unclear, "
        "and send only after they say yes. Never guess or complete an "
        "address. Plain text and links only; you cannot attach files. One "
        f"address per call, and at most {MAX_EMAILS_PER_CALL} emails per call. "
        "Hail adds a short line "
        "saying it was asked for on this call. Then say it is sent."
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
    bind=_bind,
)


__all__ = [
    "ADDRESS_PATTERN",
    "EVENT_KIND",
    "MAX_ADDRESSES_PER_CALLER_PER_DAY",
    "MAX_ADDRESS_CHARS",
    "MAX_BODY_CHARS",
    "MAX_EMAILS_PER_CALL",
    "MAX_EMAILS_PER_CALLER_PER_DAY",
    "MAX_EMAILS_PER_ORG_PER_DAY",
    "MAX_ORG_NAME_CHARS",
    "METADATA_KEY",
    "SPEC",
    "clean_body",
    "clean_subject",
    "framed_body",
    "framed_subject",
]
