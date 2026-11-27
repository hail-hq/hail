"""Voicebot → API agent-send routes.

The voice agent's send tools execute here so the full existing outbound
stack — suppression/velocity gate, funds, audit, billing — runs
unchanged (spec: docs/superpowers/specs/
2026-07-11-voicebot-agent-tools-design.md). Auth is the shared
HAIL_INTERNAL_SECRET HMAC (routes/internal/auth.py).

Responses are always HTTP 200 with ``{ok, spoken}`` — ``spoken`` is a
short plain sentence the agent says on the call. Policy denials are
data, not HTTP errors, and stay deliberately vague: never reveal
suppression-list membership or a member's address to the callee.

The agent never supplies addresses: SMS always targets the call's
counterpart (``calls.to_e164`` on outbound, ``calls.from_e164`` on inbound);
email targets a member or saved contact by name, resolved here, scoped to
the call's org, or the person the agent talks to when the model passes the
reserved ``SELF_RECIPIENT``. An address someone says or types is never a
recipient.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import ceil
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from hailhq.api.agent_email_context import (
    InboxSelection,
    conversation_inbox,
    source_files,
)
from hailhq.api.audit import write_audit_log
from hailhq.api.browser_messaging import (
    email_provider_ready,
    sms_sender,
    verified_email,
)
from hailhq.api.numbers import resolve_sms_number
from hailhq.api.routes.email_domains import get_email_provider
from hailhq.api.routes.emails import (
    deliver_email,
    resolve_hail_mail_sender,
    resolve_sender,
)
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.sms import deliver_sms
from hailhq.core.agent_caps import check_agent_send_allowed
from hailhq.core.agent_tools.send_email import (
    MAX_BODY_CHARS as EMAIL_MAX_BODY_CHARS,
)
from hailhq.core.agent_tools.send_email import (
    MAX_RECIPIENT_NAME_CHARS,
    MAX_SUBJECT_CHARS,
    caller_email_body,
    caller_email_subject,
    is_self_recipient,
)
from hailhq.core.agent_tools.send_sms import MAX_BODY_CHARS as SMS_MAX_BODY_CHARS
from hailhq.core.agent_tools.transfer_call import MAX_REASON_CHARS
from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.browser_voice import reserve_browser_preview
from hailhq.core.call_allowance import (
    HANDOVER_MIN_SECONDS,
    HANDOVER_PICKUP_SECONDS,
    HANDOVER_RING_MINUTES,
    available_cents,
    cap_seconds,
    forget_price,
    handover_dials,
    hold,
    hold_parts,
    hold_until,
    paid_minutes,
    plan_minutes_left,
    plan_split,
)
from hailhq.core.carrier_routing import PBX, voice_route
from hailhq.core.compliance_gate import (
    check_email_allowed,
    check_handover_allowed,
    check_sms_allowed,
    normalize_recipient,
)
from hailhq.core.contact_ids import normalize_contact_id, parse_contact_id
from hailhq.core.db import get_session, key_lock, org_lock
from hailhq.core.directory import (
    entry_email,
    find_by_phone,
    resolve_contact_emails,
    resolve_member_emails,
    resolve_member_phones,
)
from hailhq.core.email_agent import (
    MAX_REPLY_CHARS as EMAIL_MAX_REPLY_CHARS,
)
from hailhq.core.email_agent import (
    MAX_TOOL_SENDS,
    emails_sent_on_call,
    reply_references,
    reply_subject,
    routed_agent,
    run_refusal,
    stamp_skip,
    tool_sends_for_reply,
)
from hailhq.core.email_agent import (
    prior_reply as prior_email_reply,
)
from hailhq.core.email_agent import (
    thread_lock_key as email_thread_lock_key,
)
from hailhq.core.email_attachment_limits import MAX_EMAIL_ATTACHMENT_BYTES
from hailhq.core.email_sender import from_address_for
from hailhq.core.forward_targets import normalize_address
from hailhq.core.handover import (
    HANDOVER_ANSWERED,
    HANDOVER_EVENT_KIND,
    ON_CALL_LINE,
    HandoverPerson,
    country_of,
    handover_limit,
    handover_sms_text,
    has_answered_handover,
    load_handover,
    pbx_extension,
)
from hailhq.core.internal_webhook import quote_call_price, quotes_enabled
from hailhq.core.models import (
    Agent,
    AgentHandoverContact,
    Call,
    CallEvent,
    Email,
    EmailAttachment,
    EmailDomain,
    Organization,
    PhoneNumber,
    Sms,
)
from hailhq.core.plans import PlanUse
from hailhq.core.provider_config import load_org_provider_configs
from hailhq.core.providers.email import EmailProvider
from hailhq.core.providers.email.ses import SES_NOT_ACCEPTED_FAILURES
from hailhq.core.sms_registration_gate import us_registration_block
from hailhq.core.sms_trial import (
    TrialBodyTooLong,
    claim_trial_send,
    is_verified_trial_phone,
    trial_sender,
)
from hailhq.core.telemetry import bind_organization
from hailhq.core.telemetry_identity import tag_context
from hailhq.core.telephony_catalog import sells_in
from hailhq.core.text_agent import (
    AGENT_CAPS_SKIP,
    CARRIER_REJECTED_SKIP,
    INSUFFICIENT_FUNDS_SKIP,
    MAX_REPLIES_PER_THREAD,
    NO_AGENT_SKIP,
    PROVIDER_ERROR_SKIP,
    SEND_BLOCKED_SKIP,
    SMS_REGISTRATION_SKIP,
    THREAD_CAP_SKIP,
    answers_texts,
    prior_reply,
    replies_in_thread,
    thread_lock_key,
)
from hailhq.core.threads import is_e164
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# A phone number inside a gate sentence, hidden before it is logged.
_PHONE_IN_TEXT = re.compile(r"\+?\d[\d\s().-]{6,}\d")

router = APIRouter(
    prefix="/internal/agent",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)

_SPOKEN_CALL_UNAVAILABLE = "This call can no longer send messages."
_SPOKEN_NOT_ALLOWED = "I'm not able to send that message."
_SPOKEN_SMS_SENT = "Text message sent to the number on this call."
_SPOKEN_SMS_FAILED = "I couldn't send the text message."
_SPOKEN_SMS_UNCONFIGURED = "Text messaging isn't set up for this account."
_SPOKEN_EMAIL_SENT = "Email sent."
_SPOKEN_EMAIL_FAILED = "I couldn't send the email."
_SPOKEN_EMAIL_UNCONFIGURED = "Email isn't set up for this account."
_SPOKEN_SMS_SENT_MEMBER = "Text message sent."
_SPOKEN_RUN_UNAVAILABLE = "This email can no longer send messages."
_SPOKEN_RUN_LIMIT = "That is the limit of messages for one email."
_SPOKEN_TRIAL_TOO_LONG_FMT = "Keep trial texts to {n} characters. Nothing was sent."
# How long a queued reply counts as in flight. After this its delivery is
# unknown: SES may have accepted it before the API stopped. Never resend it.
_QUEUED_GRACE = timedelta(seconds=240)


class AgentSendBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: UUID
    tool_invocation_id: UUID


class AgentSendSmsRequest(AgentSendBase):
    body: str = Field(min_length=1, max_length=SMS_MAX_BODY_CHARS)


class AgentSendEmailRequest(AgentSendBase):
    recipient_name: str = Field(min_length=1, max_length=MAX_RECIPIENT_NAME_CHARS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body_text: str = Field(min_length=1, max_length=EMAIL_MAX_BODY_CHARS)
    reply_to_email_id: UUID | None = None
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)


class AgentSendResponse(BaseModel):
    ok: bool
    spoken: str


def _meta(req: AgentSendBase) -> dict[str, str]:
    return {
        "call_id": str(req.call_id),
        "tool_invocation_id": str(req.tool_invocation_id),
    }


async def _load_call_for_update(db: AsyncSession, call_id: UUID) -> Call | None:
    # FOR UPDATE on the call row serializes concurrent agent sends for one
    # call: the dedupe SELECT runs under this lock, so a timeout-retry
    # racing the original can't double-send. The commit that releases the
    # lock also publishes the
    # row the next request's dedupe will see; never hold the lock across
    # deliver_* — the Sms/Email INSERT + commit happens before the slow
    # provider send.
    #
    # Status is intentionally NOT checked here (that used to live in this
    # function, under the name ``_load_live_call``): the original request
    # can still be mid-provider-send when the call finalizes, so a retry
    # that arrives just after must hit the tool_invocation_id dedupe branch
    # in the caller, not a liveness denial that would misreport a send that
    # actually succeeded (or is still in flight). None only means the call
    # row itself doesn't exist.
    candidate = await db.get(Call, call_id)
    if candidate is not None and candidate.transport == "browser":
        await org_lock(db, candidate.organization_id)
    return (
        await db.execute(
            select(Call)
            .where(Call.id == call_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _tag_call(db: AsyncSession, call: Call | None) -> None:
    """Tag this request like the voicebot trace that called it."""
    if call is None:
        return
    await bind_organization(db, call.organization_id)
    tag_context(
        channel="voice",
        direction=call.direction,
        agent_id=call.agent_id,
        call_id=call.id,
        from_=call.from_e164,
        to=call.to_e164,
    )


async def _trial_may_send(db: AsyncSession, org: UUID, to: str) -> bool:
    """``to`` is the workspace's verified trial phone and a trial number is
    ready. ``claim_trial_send`` still decides."""
    return (
        await is_verified_trial_phone(db, org, to)
        and await trial_sender(db) is not None
    )


def _other_party(call: Call) -> str | None:
    """The person on the line: the caller on inbound, the callee on outbound."""
    if call.transport == "browser":
        return call.metadata_.get("browser_contacts", {}).get("sms")
    return call.from_e164 if call.direction == "inbound" else call.to_e164


async def _prior_send(
    db: AsyncSession,
    model: type[Sms | Email],
    org_id: UUID,
    tool_invocation_id: UUID,
) -> Sms | Email | None:
    """Look up an existing Sms/Email row stamped with this tool_invocation_id.

    Shared by both routes' idempotent-replay checks — a retry (timeout or
    connection-drop, see ``core/hailhq/core/agent_tools/client.py``) posts
    the exact same id, and this is how it learns the true outcome instead of
    sending again.
    """
    return (
        await db.execute(
            select(model).where(
                model.organization_id == org_id,
                model.metadata_["tool_invocation_id"].astext == str(tool_invocation_id),
            )
        )
    ).scalar_one_or_none()


async def _record_browser_outcome(db, call_id, invocation_id, message):
    # Delivery commits before this lock. Preserve any simultaneous finalize
    # or another tool's metadata when recording the external submission.
    call = await _load_call_for_update(db, call_id)
    if call is None or call.transport != "browser":
        return
    meta = dict(call.metadata_)
    meta["browser_actions"] = [
        (
            {**action, "status": message.status, "message_id": str(message.id)}
            if action.get("tool_invocation_id") == str(invocation_id)
            else action
        )
        for action in meta.get("browser_actions", [])
    ]
    call.metadata_ = meta
    await db.commit()


def _ambiguous_recipient(name: str) -> AgentSendResponse:
    return AgentSendResponse(
        ok=False,
        spoken=f"More than one person is named {name}, so I can't pick a recipient.",
    )


async def _deny(
    org_id: UUID,
    *,
    action: str,
    resource_type: str,
    spoken: str,
    payload: dict[str, Any],
) -> AgentSendResponse:
    """Write a denial audit row (api_key_id=None, resource_id=None) and
    return the ``ok=False`` spoken response. Collapses the audit-then-deny
    shape shared by the cap/funds and compliance-gate denials in both
    routes."""
    await write_audit_log(
        organization_id=org_id,
        api_key_id=None,
        action=action,
        resource_type=resource_type,
        resource_id=None,
        payload=payload,
        actor_kind="system",
    )
    return AgentSendResponse(ok=False, spoken=spoken)


# Emails one phone call may send with send_email, so one caller cannot turn a
# call into a stream of email. Browser test calls have their own limit.
MAX_CALL_EMAILS = 3
# Longest recipient name quoted back to the model in a refusal.
_MAX_QUOTED_NAME = 60

# Tool results for the model, not words for the caller: the model tells the
# person in the call's language. An unknown name and a name shared by several
# people get the same answer, so a caller cannot probe the saved contacts.
_SPOKEN_EMAIL_NO_MATCH = (
    'Not sent: no single team member or saved contact matches "{name}".'
    " You can only email people on file, to prevent spam."
)
_SPOKEN_EMAIL_NO_SELF = (
    "Not sent: no email address is on file for the person on this call."
    " You can only email people on file, to prevent spam. Offer to take a"
    " message."
)

_SPOKEN_RUN_REPLIED = (
    "Your reply to this email was already sent. Nothing more was sent."
)
_SPOKEN_RUN_REPLY_FAILED = (
    "Not sent: your reply to this email already failed. Nothing more can go"
    " to the sender as that reply."
)
_SPOKEN_CALL_EMAIL_LIMIT = (
    f"Not sent: this call already sent {MAX_CALL_EMAILS} emails, the limit for"
    " one call. Offer to take a message."
)


def _quoted_name(name: str) -> str:
    """``name`` safe to quote back to the model: one line, no double quotes,
    at most ``_MAX_QUOTED_NAME`` characters."""
    text = "".join(c if c.isprintable() else " " for c in name).replace('"', "'")
    text = " ".join(text.split())
    if len(text) > _MAX_QUOTED_NAME:
        return text[: _MAX_QUOTED_NAME - 1] + "…"
    return text


async def _known_email(
    db: AsyncSession, org: UUID, name: str
) -> str | AgentSendResponse:
    """The one address on file for ``name``: a member, else a saved contact.
    Members win over contacts, so a customer saved under a team member's name
    never blocks or takes that member's email. No match and more than one
    match get the same answer."""
    matches = await resolve_member_emails(db, org, name)
    if not matches:
        matches = await resolve_contact_emails(db, org, name)
    # One address saved twice, in any letter case, is still one recipient.
    addresses = list(dict.fromkeys(m.lower() for m in matches))
    if len(addresses) != 1:
        return AgentSendResponse(
            ok=False, spoken=_SPOKEN_EMAIL_NO_MATCH.format(name=_quoted_name(name))
        )
    return next(m for m in matches if m.lower() == addresses[0])


async def _call_recipient(
    db: AsyncSession, call: Call, name: str, meta: dict[str, str]
) -> tuple[str, bool] | AgentSendResponse:
    """The address for ``send_email`` on a phone call, and whether it is the
    caller's own address on file. The reserved self-reference reaches only
    the caller's own entry, found by their number; anything else is a name.
    Neither falls back to the other, so "email me" never picks someone who
    shares the caller's name.

    A name the caller is saved under that does not resolve to the caller's
    own address is refused like an unknown name, whether or not their entry
    has an email (the contact line tells the model to use "me"): a model
    that emails them
    by name (an older prompt, or one that ignores the tool text) would
    otherwise send the caller's request to a member or contact of the same
    name."""
    org = call.organization_id
    phone = _other_party(call)
    caller = await find_by_phone(db, org, phone) if phone else None
    own = await entry_email(db, org, phone, caller) if phone else None
    if is_self_recipient(name, on_call=True):
        if own:
            return own, True
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=_SPOKEN_EMAIL_NO_SELF,
            payload={**meta, "reason": "no_caller_email"},
        )
    recipient = await _known_email(db, org, name)
    if own and isinstance(recipient, str) and recipient.casefold() == own.casefold():
        return recipient, True
    if isinstance(recipient, AgentSendResponse) or (
        caller is not None and _same_name(caller.name, name)
    ):
        return AgentSendResponse(
            ok=False, spoken=_SPOKEN_EMAIL_NO_MATCH.format(name=_quoted_name(name))
        )
    return recipient, False


def _same_name(a: str, b: str) -> bool:
    # lower(), like the SQL name lookups in hailhq.core.directory.
    return a.strip().lower() == b.strip().lower()


async def _organization_name(db: AsyncSession, org: UUID) -> str | None:
    """The org's display name for the fixed lines of a caller email. One read
    on this session, in a savepoint. It only adds words, so it fails open:
    the website-owned table is missing on a pure self-host, and a schema
    change must not stop the email. The lines then name no organization."""
    try:
        async with db.begin_nested():
            return await db.scalar(
                select(Organization.name).where(Organization.id == org)
            )
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) != "42P01":  # undefined_table
            logger.warning("organization name lookup failed: %s", exc)
        return None


async def _call_language(db: AsyncSession, call: Call) -> str | None:
    """The call's language, as the voicebot picks it: ``voice_config.language``,
    else the org's saved TTS language (an inbound call copies the agent's
    voice_config, which may have none). The voicebot reads the TTS language
    at call start; a change during the call shows here. It only picks the
    words of the caller email's fixed lines, so a bad shape or a failed
    read means None (English); the savepoint keeps the send's transaction."""
    config = call.voice_config if isinstance(call.voice_config, dict) else {}
    language = config.get("language")
    if isinstance(language, str) and language:
        return language
    try:
        async with db.begin_nested():
            tts = (await load_org_provider_configs(db, call.organization_id)).get("tts")
    except SQLAlchemyError as exc:
        logger.warning("call language lookup failed: %s", exc)
        return None
    params = tts.params if tts is not None else None
    language = params.get("language") if isinstance(params, dict) else None
    return language if isinstance(language, str) else None


async def _run_recipient(
    db: AsyncSession, inbound: Email, name: str
) -> str | AgentSendResponse:
    """The address for ``send_email`` in an email run. The self-reference is
    the sender: ``_email_run`` already refused mail whose From address
    ingest did not prove. Anything else is a name."""
    if is_self_recipient(name):
        return normalize_address(inbound.from_address)
    return await _known_email(db, inbound.organization_id, name)


async def _send_sms_to(
    db: AsyncSession,
    org: UUID,
    *,
    recipient: str,
    body: str,
    meta: dict[str, str],
    agent_id: UUID | None,
    dialed: PhoneNumber | None,
    consent_source: str,
    route_source: str,
    route_ref: dict[str, str],
    sent: str,
) -> AgentSendResponse:
    """Gate, cap, pick a number, write the row, audit, and deliver one text.
    The caller chose ``recipient``: the person on the line, or a member."""
    gate = await check_sms_allowed(db, org, recipient)
    if not gate.allowed:
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**meta, "reason": gate.reason, "checks": gate.checks},
        )

    # Platform agent-caps gate (velocity + kill switch): voicebot sends must
    # count toward the same per-recipient caps the public routes enforce —
    # no-op for human-origin orgs. Same recipient set as the gate above.
    cap_denial = await check_agent_send_allowed(db, org, "sms", [recipient])
    if cap_denial is not None:
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**meta, "reason": "agent_caps", "detail": cap_denial.reason},
        )

    # Text from the number the caller dialed when it can text; else a text
    # number of this agent (see resolve_sms_number).
    from_number = await resolve_sms_number(db, org, agent_id, dialed, recipient)
    block = (
        await us_registration_block(db, org, from_number, recipient)
        if from_number is not None
        else None
    )
    # The agent's own number, kept for the bind audit when the trial number
    # sends instead: the bind commits with the text.
    own_number = from_number
    auto_bound = (
        from_number is not None
        and db.info.pop("auto_bound_sms_number_id", None) == from_number.id
    )
    row_meta = meta
    if from_number is None or block is not None:
        # Trial text to the workspace's verified phone. A refused claim keeps
        # today's answer and is not rolled back: this transaction may hold
        # the auto-bind and send-cap rows, which a refusal never commits.
        try:
            trial = await claim_trial_send(
                db, org, to=recipient, body=body, limit=SMS_MAX_BODY_CHARS
            )
        except TrialBodyTooLong as exc:
            return AgentSendResponse(
                ok=False, spoken=_SPOKEN_TRIAL_TOO_LONG_FMT.format(n=exc.max_chars)
            )
        if trial is None:
            if from_number is None:
                return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)
            return await _deny(
                org,
                action="agent.sms.blocked",
                resource_type="sms",
                spoken=_SPOKEN_NOT_ALLOWED,
                payload={**meta, "reason": block},
            )
        # The claim holds the workspace lock until the commit below.
        from_number = trial.number
        body = trial.body
        row_meta = {**meta, **trial.metadata}

    sms = Sms(
        organization_id=org,
        provider=from_number.provider,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=recipient,
        agent_id=agent_id,
        direction="outbound",
        status="queued",
        body=body,
        metadata_=row_meta,
    )
    db.add(sms)
    await db.commit()

    if auto_bound:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="number.route",
            resource_type="phone_number",
            resource_id=own_number.id,
            payload={
                "e164": own_number.e164,
                "sms_agent_id": str(agent_id),
                "automatic": True,
                "source": route_source,
                **route_ref,
            },
            actor_kind="system",
        )

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.sms.send",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            **meta,
            "to": sms.to_e164,
            "consent_source": consent_source,
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )

    err = await deliver_sms(db, sms)
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.send_failed",
            resource_type="sms",
            resource_id=sms.id,
            payload={**meta, "end_reason": err},
            actor_kind="system",
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_FAILED)
    return AgentSendResponse(ok=True, spoken=sent)


async def _send_email_to(
    db: AsyncSession,
    email_provider: EmailProvider,
    org: UUID,
    *,
    recipient: str,
    subject: str,
    body_text: str,
    meta: dict[str, Any],
    agent_id: UUID | None,
    consent_source: str,
    reply_parent: Email | None = None,
    reply_domain: EmailDomain | None = None,
    attachment_rows: list[EmailAttachment] | None = None,
) -> AgentSendResponse:
    """Gate, cap, pick the sender, write the row, audit, and deliver one
    email to a member or saved contact the caller resolved by name."""
    gate = await check_email_allowed(db, org, [recipient])
    if not gate.allowed:
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**meta, "reason": gate.reason, "checks": gate.checks},
        )

    # Same agent-caps gate as the public /emails route — one normalized
    # recipient for voicebot sends (no cc/bcc fan-out on this path).
    cap_denial = await check_agent_send_allowed(
        db, org, "email", [normalize_recipient(recipient)]
    )
    if cap_denial is not None:
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**meta, "reason": "agent_caps", "detail": cap_denial.reason},
        )

    try:
        # An agent cannot name a sending domain. With several verified custom
        # domains this path takes the oldest, where POST /emails returns 422
        # and the caller chooses. A verified custom domain still wins over the
        # hail-mail address, as it does on POST /emails.
        sd = reply_domain or await resolve_sender(db, org, None, allow_ambiguous=True)
    except HTTPException:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_UNCONFIGURED)

    email = Email(
        organization_id=org,
        email_domain_id=sd.id,
        agent_id=agent_id,
        from_address=from_address_for(sd, None),
        to_addresses=[recipient],
        subject=subject,
        body_text=body_text,
        status="queued",
        provider="ses",
        metadata_=meta,
    )
    if reply_parent is not None:
        email.subject = reply_subject(reply_parent.subject)
        email.in_reply_to, email.references_ids = reply_references(reply_parent)
    db.add(email)
    await db.commit()
    await db.refresh(email)

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action=(
            "agent.email.reply" if meta.get("reply_to_email_id") else "agent.email.send"
        ),
        resource_type="email",
        resource_id=email.id,
        payload={
            **meta,
            "to": email.to_addresses,
            "subject": email.subject,
            "consent_source": consent_source,
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )

    err = await deliver_email(
        db,
        email_provider,
        email,
        at_most_once=True,
        **({"attachment_rows": attachment_rows} if attachment_rows else {}),
    )
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.send_failed",
            resource_type="email",
            resource_id=email.id,
            payload={**meta, "end_reason": err},
            actor_kind="system",
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_EMAIL_SENT)


async def _shared_denial(
    db: AsyncSession, call: Call, use: PlanUse, to: str | None = None
) -> tuple[str, str] | None:
    """The funds check shared by the send routes. Each send is billed like
    any text or email; phone-call emails also have ``MAX_CALL_EMAILS``.
    ``use`` and the text's destination ``to`` let the plan count
    (hailhq.core.plans).

    Returns ``(spoken, audit_reason)`` on denial or None when the send may
    proceed. The audit reason distinguishes which gate fired; the spoken
    text stays vague for the callee.
    """
    if (
        call.metadata_.get(CALL_META_BILLED)
        or (call.transport == "browser" and not call.metadata_.get("browser_trial"))
    ) and not await has_funds(
        db, call.organization_id, use, country=country_of(to) if to else None
    ):
        return _SPOKEN_NOT_ALLOWED, "insufficient_funds"
    return None


@router.post("/send-sms", response_model=AgentSendResponse)
async def agent_send_sms(
    body: AgentSendSmsRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentSendResponse:
    call = await _load_call_for_update(db, body.call_id)
    await _tag_call(db, call)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    # Idempotent replay: the voicebot retries timeouts/connection-drops with
    # the same id. Checked BEFORE the liveness gate below — the original
    # request can still be mid-provider-send when the call finalizes, so a
    # retry that arrives just after must learn the true outcome here rather
    # than being denied as "call ended".
    prior = await _prior_send(db, Sms, org, body.tool_invocation_id)
    if prior is not None:
        # queued = committed-but-in-flight (the concurrent original holds
        # it between commit and delivery reconciliation); optimistic
        # success avoids the duplicate-send failure mode, which is the
        # worse error on a live call.
        ok = prior.status not in ("failed", "undelivered")
        return AgentSendResponse(
            ok=ok,
            spoken=(
                "The text is still being submitted. Do not send it again."
                if prior.status == "queued"
                else _SPOKEN_SMS_SENT if ok else _SPOKEN_SMS_FAILED
            ),
        )

    if call.status != "in_progress":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)

    if call.transport == "browser" and len(body.body.encode("utf-16-le")) // 2 > 70:
        return AgentSendResponse(
            ok=False,
            spoken="Keep test texts to 70 characters. Emoji count twice. Nothing was sent.",
        )

    counterpart = _other_party(call)
    if call.transport == "browser" and not counterpart:
        return AgentSendResponse(
            ok=False,
            spoken="Enter your mobile number and agree to receive test texts before starting a new conversation.",
        )

    denial = await _shared_denial(db, call, "text", counterpart)
    if denial is not None:
        spoken, reason = denial
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=spoken,
            payload={**_meta(body), "reason": reason},
        )

    # Text from the number the caller dialed when it can text; else a text
    # number of this agent (see resolve_sms_number).
    dialed = (
        await db.get(PhoneNumber, call.to_number_id)
        if call.direction == "inbound" and call.to_number_id is not None
        else None
    )
    if call.transport != "browser":
        return await _send_sms_to(
            db,
            org,
            recipient=counterpart,  # the person on the line — never a parameter
            body=body.body,
            meta=_meta(body),
            agent_id=call.agent_id,
            dialed=dialed,
            consent_source="voice_call",
            route_source="in-call send_sms",
            route_ref={"call_id": str(call.id)},
            sent=_SPOKEN_SMS_SENT,
        )

    gate = await check_sms_allowed(db, org, counterpart)
    if not gate.allowed:
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )

    from_number = await sms_sender(db, org, counterpart, call=call)
    block = (
        await us_registration_block(db, org, from_number, counterpart)
        if from_number is not None
        else None
    )
    # The hail.so demo keeps its own senders. Other conversations may send a
    # trial text, claimed after the reservation below.
    use_trial = (from_number is None or block is not None) and not call.metadata_.get(
        "browser_trial"
    )
    if from_number is None and not use_trial:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)
    if block is not None and not use_trial:
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**_meta(body), "reason": block},
        )

    auto_bound = (
        from_number is not None
        and db.info.pop("auto_bound_sms_number_id", None) == from_number.id
    )

    if call.transport == "browser" and not await reserve_browser_preview(
        db,
        call.id,
        {
            "channel": "sms",
            "recipient": counterpart,
            "body": body.body,
            "preview": False,
            "tool_invocation_id": str(body.tool_invocation_id),
            "status": "queued",
        },
    ):
        return AgentSendResponse(
            ok=False,
            spoken="Your three test texts are used, or the conversation has ended. Nothing was sent.",
        )

    own_number = from_number
    sms_body = body.body
    trial_meta = {}
    if use_trial:
        # The reservation took the same workspace lock and only flushed, so
        # the lock holds until the insert below commits. A refused claim
        # keeps today's answer and is not rolled back: nothing commits on
        # that path, so the flushed reservation is dropped with it.
        # The body has at most 70 characters, so it always fits.
        trial = await claim_trial_send(
            db, org, to=counterpart, body=body.body, limit=SMS_MAX_BODY_CHARS
        )
        if trial is None:
            if from_number is None:
                return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)
            return await _deny(
                org,
                action="agent.sms.blocked",
                resource_type="sms",
                spoken=_SPOKEN_NOT_ALLOWED,
                payload={**_meta(body), "reason": block},
            )
        from_number = trial.number
        sms_body = trial.body
        trial_meta = trial.metadata

    sms = Sms(
        organization_id=org,
        provider=from_number.provider,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=counterpart,  # the person on the line — never a parameter
        agent_id=call.agent_id,
        direction="outbound",
        status="queued",
        body=sms_body,
        metadata_={
            **_meta(body),
            **(
                {"browser_trial": True}
                if call.transport == "browser" and call.metadata_.get("browser_trial")
                else {}
            ),
            **trial_meta,
        },
    )
    db.add(sms)
    await db.commit()

    if auto_bound:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="number.route",
            resource_type="phone_number",
            resource_id=own_number.id,
            payload={
                "e164": own_number.e164,
                "sms_agent_id": str(call.agent_id),
                "automatic": True,
                "source": "in-call send_sms",
                "call_id": str(call.id),
            },
            actor_kind="system",
        )

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.sms.send",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            **_meta(body),
            "to": sms.to_e164,
            "consent_source": "voice_call",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )

    err = await deliver_sms(
        db,
        sms,
        **(
            {"billable": False}
            if call.transport == "browser" and call.metadata_.get("browser_trial")
            else {}
        ),
    )
    await _record_browser_outcome(db, call.id, body.tool_invocation_id, sms)
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.send_failed",
            resource_type="sms",
            resource_id=sms.id,
            payload={**_meta(body), "end_reason": err},
            actor_kind="system",
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_SMS_SENT)


class AgentReplySmsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sms_id: UUID  # the inbound row being answered
    body: str = Field(min_length=1, max_length=SMS_MAX_BODY_CHARS)


class AgentReplySmsResponse(BaseModel):
    ok: bool
    # done | skipped | failed: what the worker writes to agent_reply_state.
    state: str
    # A short code the worker stores as sms.metadata.skipped_reason; the
    # console names it. ``detail`` is a customer-safe sentence for a code that
    # has one (no phone numbers).
    reason: str | None = None
    detail: str | None = None
    reply_id: UUID | None = None


@router.post("/reply-sms", response_model=AgentReplySmsResponse)
async def agent_reply_sms(
    body: AgentReplySmsRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentReplySmsResponse:
    """Send a text agent's reply to an inbound SMS.

    The text worker (``hailhq.voicebot.textbot``) wrote the body; this route
    owns everything that must stay server-side: the thread cap, funds,
    suppression and velocity gates, billing, audit, delivery. Replies go out
    from the number the person wrote to, through its own carrier.
    """
    inbound = await db.get(Sms, body.sms_id)
    if inbound is None or inbound.direction != "inbound":
        return AgentReplySmsResponse(ok=False, state="failed", reason="unknown_sms")
    org = inbound.organization_id
    await bind_organization(db, org)
    tag_context(
        channel="sms",
        direction="inbound",
        sms_id=inbound.id,
        from_=inbound.from_e164,
        to=inbound.to_e164,
    )
    number = (
        await db.get(PhoneNumber, inbound.to_number_id)
        if inbound.to_number_id
        else None
    )
    agent = (
        await db.get(Agent, number.sms_agent_id)
        if number is not None and number.sms_agent_id
        else None
    )
    # The agent may have been paused or muted while the worker wrote the reply.
    if (
        number is None
        or agent is None
        or not answers_texts(agent)
        or number.provisioning_state != "active"
    ):
        return AgentReplySmsResponse(ok=False, state="skipped", reason=NO_AGENT_SKIP)

    # One request per thread at a time: the lock is held until the reply row
    # is committed, so a retry that races the original sees it below and the
    # per-thread reply cap below is exact. A lock on the inbound row (FOR
    # UPDATE) would deadlock with the worker's claim, hence an advisory lock
    # keyed by thread.
    await key_lock(db, thread_lock_key(inbound))
    # Idempotent: the worker retries a timed-out POST with the same sms_id.
    # The oldest row wins, a belt for rows written before the lock existed.
    prior = await prior_reply(db, inbound)
    if prior is not None:
        return AgentReplySmsResponse(ok=True, state="done", reply_id=prior.id)

    if await replies_in_thread(db, inbound) >= MAX_REPLIES_PER_THREAD:
        return AgentReplySmsResponse(ok=False, state="skipped", reason=THREAD_CAP_SKIP)
    if not await has_funds(db, org, "text", country=country_of(inbound.from_e164)):
        return AgentReplySmsResponse(
            ok=False, state="skipped", reason=INSUFFICIENT_FUNDS_SKIP
        )
    gate = await check_sms_allowed(db, org, inbound.from_e164)
    if not gate.allowed:
        # The gate's sentence can hold the recipient's number: it is not
        # returned or stored, and the log shows it with the number hidden.
        logger.info(
            "sms_id=%s agent reply blocked: %s",
            inbound.id,
            _PHONE_IN_TEXT.sub("[number]", gate.reason or ""),
        )
        return AgentReplySmsResponse(
            ok=False, state="skipped", reason=SEND_BLOCKED_SKIP
        )
    cap_denial = await check_agent_send_allowed(db, org, "sms", [inbound.from_e164])
    if cap_denial is not None:
        return AgentReplySmsResponse(ok=False, state="skipped", reason=AGENT_CAPS_SKIP)
    block = await us_registration_block(db, org, number, inbound.from_e164)
    if block is not None:
        return AgentReplySmsResponse(
            ok=False, state="skipped", reason=SMS_REGISTRATION_SKIP, detail=block
        )

    reply = Sms(
        organization_id=org,
        provider=number.provider,
        from_number_id=number.id,
        agent_id=agent.id,
        from_e164=number.e164,
        to_e164=inbound.from_e164,
        direction="outbound",
        status="queued",
        body=body.body[:SMS_MAX_BODY_CHARS],
        metadata_={"reply_to_sms_id": str(inbound.id), "agent_id": str(agent.id)},
    )
    db.add(reply)
    await db.commit()
    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.sms.reply",
        resource_type="sms",
        resource_id=reply.id,
        payload={
            "reply_to": str(inbound.id),
            "agent_id": str(agent.id),
            "to": reply.to_e164,
            "consent_source": "inbound_sms",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )
    err = await deliver_sms(db, reply)
    if err is not None:
        # A carrier's own error code stays on the reply row (``error_code``).
        code = (
            PROVIDER_ERROR_SKIP if err == PROVIDER_ERROR_SKIP else CARRIER_REJECTED_SKIP
        )
        return AgentReplySmsResponse(
            ok=False, state="failed", reason=code, reply_id=reply.id
        )
    return AgentReplySmsResponse(ok=True, state="done", reply_id=reply.id)


class AgentReplyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_id: UUID  # the inbound row being answered
    # Omitted when the reply already exists: the stored text is delivered.
    body: str | None = Field(
        default=None, min_length=1, max_length=EMAIL_MAX_REPLY_CHARS
    )


class AgentReplyEmailResponse(BaseModel):
    ok: bool
    # done | skipped | failed: what the worker writes to agent_reply_state.
    # retry: try again later, a send failed for now or may still be running.
    state: str
    reason: str | None = None
    reply_id: UUID | None = None
    # With retry: wait at least this long. None means the worker's own backoff.
    retry_after_seconds: int | None = None


def _send_failure(reason: str, reply: Email) -> AgentReplyEmailResponse:
    """The worker keeps a safe failure for another attempt, and settles it as
    failed after its last one."""
    state = "retry" if reason in SES_NOT_ACCEPTED_FAILURES else "failed"
    return AgentReplyEmailResponse(
        ok=False, state=state, reason=reason, reply_id=reply.id
    )


def _in_flight(reply: Email, wait: timedelta) -> AgentReplyEmailResponse:
    return AgentReplyEmailResponse(
        ok=False,
        state="retry",
        reason="in_flight",
        reply_id=reply.id,
        retry_after_seconds=ceil(wait.total_seconds()),
    )


def _finished_reply(row: Email) -> AgentReplyEmailResponse | None:
    """The answer for a reply row whose send is over, or None while it still
    needs sending. Not just "sent": see the same reasoning in agent_send_email.
    A failed row needs sending only if SES proved it took nothing."""
    if row.status == "queued":
        return None
    if row.status == "failed":
        if row.end_reason in SES_NOT_ACCEPTED_FAILURES:
            return None
        return AgentReplyEmailResponse(
            ok=False, state="failed", reason=row.end_reason, reply_id=row.id
        )
    ok = row.status != "bounced"
    return AgentReplyEmailResponse(
        ok=ok,
        state="done" if ok else "failed",
        reason=None if ok else "bounced",
        reply_id=row.id,
    )


async def _routed_email(
    db: AsyncSession, inbound: Email
) -> tuple[EmailDomain, Agent] | None:
    """The inbound row's address and the agent that answers it now, or None.
    Routing can change while the model writes."""
    routed = await routed_agent(db, inbound)
    if routed is None or inbound.agent_id != routed[1].id:
        return None
    await bind_organization(db, inbound.organization_id)
    tag_context(
        channel="email",
        direction="inbound",
        agent_id=routed[1].id,
        email_id=inbound.id,
        from_=inbound.from_address,
        to=from_address_for(routed[0]),
    )
    return routed


@router.post("/reply-email", response_model=AgentReplyEmailResponse)
async def agent_reply_email(
    body: AgentReplyEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentReplyEmailResponse:
    """Send an agent's reply to an inbound email.

    The worker (``hailhq.voicebot.emailbot``) wrote the body. This route owns
    everything that must stay server-side: scope, thread cap, funds,
    suppression, billing, audit, and delivery. The reply goes to the ``From``
    address, never to ``Reply-To``: only ``From`` was authenticated.
    """

    async def skip(reason: str | None) -> AgentReplyEmailResponse:
        await stamp_skip(db, body.email_id, reason or "refused")
        return AgentReplyEmailResponse(ok=False, state="skipped", reason=reason)

    inbound = await db.get(Email, body.email_id)
    if inbound is None or inbound.direction != "inbound":
        return await skip("no_agent")

    # One request per thread at a time: a retry that races the original sees
    # the reply below, and the thread cap is exact.
    await key_lock(db, email_thread_lock_key(inbound))
    await db.refresh(inbound)
    # Idempotent per inbound row: the worker retries with the same email_id.
    prior = await prior_email_reply(db, inbound)
    if prior is not None and (finished := _finished_reply(prior)) is not None:
        return finished
    if prior is not None and prior.status == "queued":
        age = datetime.now(timezone.utc) - prior.requested_at
        if age < _QUEUED_GRACE:
            # The original may still be in the provider call: sending now could
            # deliver twice. The worker comes back when the grace ends.
            return _in_flight(prior, _QUEUED_GRACE - age)

    # After the checks above: the route can go while the first send is running,
    # and that send must not be reported as skipped.
    routed = await _routed_email(db, inbound)
    if routed is None:
        return await skip("no_agent")
    domain, agent = routed
    if prior is None and inbound.agent_reply_state != "processing":
        # The worker lost its claim: the row expired, failed, or was settled.
        # Its final state stands, so nothing is stamped.
        return AgentReplyEmailResponse(ok=False, state="skipped", reason="not_claimed")
    org = inbound.organization_id
    recipient = normalize_address(inbound.from_address)

    refusal = await run_refusal(db, domain, inbound, check_cap=prior is None)
    if refusal is not None:
        return await skip(refusal)
    gate = await check_email_allowed(db, org, [recipient])
    if not gate.allowed:
        return await skip(gate.reason)
    cap_denial = await check_agent_send_allowed(
        db, org, "email", [normalize_recipient(recipient)]
    )
    if cap_denial is not None:
        return await skip("agent_caps")

    if prior is not None and prior.status == "queued":
        # Age cannot prove that SES rejected a send. Settle the uncertainty
        # without another provider call, and revoke the old request's claim.
        settled_at = datetime.now(timezone.utc)
        settled = await db.execute(
            update(Email)
            .where(
                Email.id == prior.id,
                Email.status == "queued",
                Email.requested_at == prior.requested_at,
            )
            .values(
                status="failed",
                end_reason="DeliveryUnknown",
                failed_at=settled_at,
                requested_at=settled_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        await db.refresh(prior)
        if not settled.rowcount:
            return _finished_reply(prior) or _in_flight(prior, _QUEUED_GRACE)
        return _send_failure("DeliveryUnknown", prior)

    if prior is not None:
        # Only a persisted failure proving SES took nothing reaches this path.
        # The row exists, so the audit row does too: only the send is missing.
        # requested_at restarts the grace above, so a retry during a slow resend
        # waits. The guard means one claim wins even without the thread lock.
        claimed_at = datetime.now(timezone.utc)
        claim = await db.execute(
            update(Email)
            .where(
                Email.id == prior.id,
                Email.status == prior.status,
                Email.requested_at == prior.requested_at,
            )
            .values(
                status="queued",
                end_reason=None,
                failed_at=None,
                requested_at=claimed_at,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if not claim.rowcount:  # type: ignore[attr-defined]
            # Another request got there first. Report how it ended, or try later.
            await db.refresh(prior)
            return _finished_reply(prior) or _in_flight(prior, _QUEUED_GRACE)
        await db.refresh(prior)
        files = []
        if prior.metadata_.get("source_attachment_ids"):
            try:
                ids = [
                    UUID(value) for value in prior.metadata_["source_attachment_ids"]
                ]
                source_id = UUID(
                    prior.metadata_.get("attachment_reply_to_email_id", str(inbound.id))
                )
                _, _, files = await source_files(db, inbound, source_id, ids)
            except (ValueError, TypeError):
                prior.status = "failed"
                prior.end_reason = "AttachmentsUnavailable"
                prior.failed_at = datetime.now(timezone.utc)
                await db.commit()
                return _send_failure("AttachmentsUnavailable", prior)
        err = await deliver_email(
            db,
            email_provider,
            prior,
            claimed_at=claimed_at,
            at_most_once=True,
            **({"attachment_rows": files} if files else {}),
        )
        if err is not None:
            return _send_failure(err, prior)
        return AgentReplyEmailResponse(ok=True, state="done", reply_id=prior.id)

    if body.body is None:
        # Only the worker's retry omits the body, and it saw a reply row.
        return AgentReplyEmailResponse(ok=False, state="failed", reason="no_body")

    in_reply_to, references = reply_references(inbound)
    reply = Email(
        organization_id=org,
        email_domain_id=domain.id,
        agent_id=agent.id,
        # Never To or Cc: the sender writes those headers.
        from_address=from_address_for(domain),
        from_name=agent.name,
        to_addresses=[recipient],
        subject=reply_subject(inbound.subject),
        body_text=body.body,
        status="queued",
        provider="ses",
        in_reply_to=in_reply_to,
        references_ids=references,
        metadata_={"reply_to_email_id": str(inbound.id), "agent_id": str(agent.id)},
    )
    db.add(reply)
    await db.commit()
    await db.refresh(reply)
    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.email.reply",
        resource_type="email",
        resource_id=reply.id,
        payload={
            "reply_to": str(inbound.id),
            "agent_id": str(agent.id),
            "to": reply.to_addresses,
            "consent_source": "inbound_email",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )
    err = await deliver_email(
        db, email_provider, reply, claimed_at=reply.requested_at, at_most_once=True
    )
    if err is not None:
        return _send_failure(err, reply)
    return AgentReplyEmailResponse(ok=True, state="done", reply_id=reply.id)


class AgentEmailRunBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inbound_email_id: UUID  # the email the agent is answering
    tool_invocation_id: UUID
    recipient_name: str = Field(min_length=1, max_length=MAX_RECIPIENT_NAME_CHARS)


class AgentEmailRunEmailRequest(AgentEmailRunBase):
    reply_to_email_id: UUID | None = None
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=10)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body_text: str = Field(min_length=1, max_length=EMAIL_MAX_BODY_CHARS)


class AgentEmailRunSmsRequest(AgentEmailRunBase):
    body: str = Field(min_length=1, max_length=SMS_MAX_BODY_CHARS)


def _email_run_meta(
    body: AgentEmailRunBase, agent: Agent, inbound: Email
) -> dict[str, str]:
    return {
        "inbound_email_id": str(body.inbound_email_id),
        "tool_invocation_id": str(body.tool_invocation_id),
        "agent_id": str(agent.id),
        # Counts toward the sender's thread cap.
        "thread_sender": normalize_address(inbound.from_address),
    }


def _replayed_send(prior: Email | Sms) -> AgentSendResponse:
    """The answer for a send that already has a row. See agent_send_email for
    why a queued row counts as sent."""
    if isinstance(prior, Email):
        ok = prior.status not in ("failed", "bounced")
        return AgentSendResponse(
            ok=ok, spoken=_SPOKEN_EMAIL_SENT if ok else _SPOKEN_EMAIL_FAILED
        )
    ok = prior.status not in ("failed", "undelivered")
    return AgentSendResponse(
        ok=ok, spoken=_SPOKEN_SMS_SENT_MEMBER if ok else _SPOKEN_SMS_FAILED
    )


async def _email_run(
    db: AsyncSession,
    body: AgentEmailRunBase,
    *,
    model: type[Email | Sms],
    action: str,
    resource_type: str,
) -> tuple[Email, Agent] | AgentSendResponse:
    """Everything a tool send in an email run must pass before it picks a
    recipient. Only a claimed row runs tools. Scope, funds, and the thread cap
    are read again under the thread lock.

    The retry of a send is answered first, under the lock, so it sees the row: a
    send can take the last slot of the cap, or outlive the claim, and the retry
    must still learn that it went out.
    """
    inbound = await db.get(Email, body.inbound_email_id)
    if inbound is None or inbound.direction != "inbound":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_UNAVAILABLE)
    await key_lock(db, email_thread_lock_key(inbound))
    await db.refresh(inbound)
    prior = await _prior_send(
        db, model, inbound.organization_id, body.tool_invocation_id
    )
    if prior is not None:
        return _replayed_send(prior)
    routed = await _routed_email(db, inbound)
    if routed is None or inbound.agent_reply_state != "processing":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_UNAVAILABLE)
    domain, agent = routed
    refusal = await run_refusal(db, domain, inbound, count_own_sends=True)
    if refusal == "thread_cap":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_LIMIT)
    if refusal == "insufficient_funds":
        return await _deny(
            inbound.organization_id,
            action=action,
            resource_type=resource_type,
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**_email_run_meta(body, agent, inbound), "reason": refusal},
        )
    if refusal is not None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_UNAVAILABLE)
    return inbound, agent


@router.post("/email-run/send-email", response_model=AgentSendResponse)
async def agent_email_run_send_email(
    body: AgentEmailRunEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentSendResponse:
    """The ``send_email`` tool in an email run: a member or saved contact by
    name."""
    run = await _email_run(
        db, body, model=Email, action="agent.email.blocked", resource_type="email"
    )
    if isinstance(run, AgentSendResponse):
        return run
    inbound, agent = run
    org = inbound.organization_id

    parent = None
    files = []
    meta = _email_run_meta(body, agent, inbound)
    recipient = await _run_recipient(db, inbound, body.recipient_name)
    if isinstance(recipient, AgentSendResponse):
        return recipient
    # A send with original files is the run's reply itself: threaded, from
    # the inbound's domain, and seen by the reply endpoint, which then sends
    # no second email. Any other send, also to the sender, is its own email.
    if body.attachment_ids or body.reply_to_email_id:
        # A retry of this same send was answered in _email_run, so a reply
        # found here is another send: it already used the run's one reply.
        # One that failed is reported as failed, so the model says so.
        existing = await prior_email_reply(db, inbound)
        if existing is not None:
            if existing.status in ("failed", "bounced"):
                return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_REPLY_FAILED)
            return AgentSendResponse(ok=True, spoken=_SPOKEN_RUN_REPLIED)
        try:
            scope, source, files = await source_files(
                db, inbound, body.reply_to_email_id or inbound.id, body.attachment_ids
            )
        except ValueError:
            return AgentSendResponse(
                ok=False,
                spoken="Those files are not available in this email thread. Nothing was sent.",
            )
        if normalize_address(recipient) != scope.peer:
            return AgentSendResponse(
                ok=False,
                spoken="Original files can only return to this email's sender.",
            )
        parent = inbound
        # prior_reply matches the normalized sender address exactly.
        recipient = scope.peer
        meta = {
            **meta,
            "reply_to_email_id": str(inbound.id),
            "attachment_reply_to_email_id": str(source.id),
            "source_attachment_ids": [str(row.id) for row in files],
        }

    if await tool_sends_for_reply(db, inbound) >= MAX_TOOL_SENDS:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_LIMIT)
    return await _send_email_to(
        db,
        email_provider,
        org,
        recipient=recipient,
        subject=body.subject,
        body_text=body.body_text,
        meta=meta,
        agent_id=agent.id,
        consent_source="inbound_email",
        reply_parent=parent,
        # Mail to the sender comes from the domain they wrote to.
        reply_domain=(
            await db.get(EmailDomain, inbound.email_domain_id)
            if parent is not None
            or normalize_address(recipient) == normalize_address(inbound.from_address)
            else None
        ),
        attachment_rows=files,
    )


@router.post("/email-run/send-sms", response_model=AgentSendResponse)
async def agent_email_run_send_sms(
    body: AgentEmailRunSmsRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentSendResponse:
    """The ``send_sms`` tool in an email run: a member by directory name."""
    run = await _email_run(
        db, body, model=Sms, action="agent.sms.blocked", resource_type="sms"
    )
    if isinstance(run, AgentSendResponse):
        return run
    inbound, agent = run
    org = inbound.organization_id

    matches = await resolve_member_phones(db, org, body.recipient_name)
    if not any(matches):
        return AgentSendResponse(
            ok=False,
            spoken=f"I couldn't find a number for {body.recipient_name}.",
        )
    if len(matches) > 1:
        return _ambiguous_recipient(body.recipient_name)
    recipient = matches[0]
    assert recipient is not None

    if await tool_sends_for_reply(db, inbound) >= MAX_TOOL_SENDS:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_RUN_LIMIT)
    return await _send_sms_to(
        db,
        org,
        recipient=recipient,
        body=body.body,
        meta=_email_run_meta(body, agent, inbound),
        agent_id=agent.id,
        dialed=None,
        consent_source="inbound_email",
        route_source="email reply send_sms",
        route_ref={"email_id": str(inbound.id)},
        sent=_SPOKEN_SMS_SENT_MEMBER,
    )


@router.post("/send-email", response_model=AgentSendResponse)
async def agent_send_email(
    body: AgentSendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentSendResponse:
    call = await _load_call_for_update(db, body.call_id)
    await _tag_call(db, call)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    # Idempotent replay: checked BEFORE the liveness gate below — the
    # original request can still be mid-provider-send when the call
    # finalizes, so a retry that arrives just after must learn the true
    # outcome here rather than being denied as "call ended".
    prior = await _prior_send(db, Email, org, body.tool_invocation_id)
    if prior is not None:
        # Not just "sent": "delivered" is reachable via the SES delivery
        # webhook (core/hailhq/core/email_delivery_events.py), and
        # queued = committed-but-in-flight (the concurrent original holds
        # it between commit and delivery reconciliation). Optimistic
        # success avoids the duplicate-send failure mode, which is the
        # worse error on a live call. "bounced" is excluded too — the SES
        # webhook can flip sent→bounced between retry attempts, and a
        # bounce means the message didn't reach the recipient. "complained"
        # stays ok: the send itself succeeded, the recipient just flagged it
        # afterward. Mirrors the SMS path's failed/undelivered exclusion.
        ok = prior.status not in ("failed", "bounced")
        return AgentSendResponse(
            ok=ok,
            spoken=(
                "The email is still being submitted. Do not send it again."
                if prior.status == "queued"
                else _SPOKEN_EMAIL_SENT if ok else _SPOKEN_EMAIL_FAILED
            ),
        )

    if call.status != "in_progress":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)

    denial = await _shared_denial(db, call, "email")
    if denial is not None:
        spoken, reason = denial
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=spoken,
            payload={**_meta(body), "reason": reason},
        )

    if call.transport != "browser":
        # Counted under the call row lock (_load_call_for_update) and before
        # the commit that writes the new row, so concurrent sends on one call
        # cannot both take the last slot.
        if await emails_sent_on_call(db, org, call.id) >= MAX_CALL_EMAILS:
            return await _deny(
                org,
                action="agent.email.blocked",
                resource_type="email",
                spoken=_SPOKEN_CALL_EMAIL_LIMIT,
                payload={**_meta(body), "reason": "call_email_limit"},
            )
        resolved = await _call_recipient(db, call, body.recipient_name, _meta(body))
        if isinstance(resolved, AgentSendResponse):
            return resolved
        recipient, to_caller = resolved
        subject, body_text = body.subject, body.body_text
        if to_caller:
            # The caller's own address on file, which caller ID found and can
            # be faked: fixed words in the subject, first line and footer say
            # why it came.
            org_name = await _organization_name(db, org)
            language = await _call_language(db, call)
            subject = caller_email_subject(subject, org_name, language)
            body_text = caller_email_body(body_text, org_name, language)
        parent = None
        files = []
        meta = _meta(body)
        if body.reply_to_email_id or body.attachment_ids:
            try:
                scope, parent, files = await source_files(
                    db, call, body.reply_to_email_id, body.attachment_ids
                )
            except ValueError:
                return AgentSendResponse(
                    ok=False,
                    spoken="Those files are not available in this conversation.",
                )
            if recipient.casefold() != scope.peer.casefold():
                return AgentSendResponse(
                    ok=False,
                    spoken="Original files can only return to the person in this conversation.",
                )
            meta = {
                **meta,
                "source_attachment_ids": [str(row.id) for row in files],
                "attachment_reply_to_email_id": str(parent.id),
            }
        return await _send_email_to(
            db,
            email_provider,
            org,
            recipient=recipient,
            subject=subject,
            body_text=body_text,
            meta=meta,
            agent_id=None,
            consent_source="voice_call",
            reply_parent=parent,
            attachment_rows=files,
        )

    if call.transport == "browser":
        if not await email_provider_ready():
            return AgentSendResponse(
                ok=False, spoken="Email sending is not configured for this test."
            )
        recipient = await verified_email(
            db, org, call.metadata_.get("browser_contacts", {}).get("email")
        )
        if not recipient:
            return AgentSendResponse(
                ok=False, spoken="Verify your account email before testing email."
            )

    gate = await check_email_allowed(db, org, [recipient])
    if not gate.allowed:
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )

    # Same agent-caps gate as the public /emails route — one normalized
    # recipient for voicebot sends (no cc/bcc fan-out on this path).
    cap_denial = await check_agent_send_allowed(
        db, org, "email", [normalize_recipient(recipient)]
    )
    if cap_denial is not None:
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={
                **_meta(body),
                "reason": "agent_caps",
                "detail": cap_denial.reason,
            },
        )

    try:
        # A voice agent cannot name a sending domain mid-call. With several
        # verified custom domains this path takes the oldest, where POST
        # /emails returns 422 and the caller chooses. A verified custom domain
        # still wins over the hail-mail address, as it does on POST /emails.
        sd = (
            await resolve_hail_mail_sender(db, org)
            if call.transport == "browser"
            else await resolve_sender(db, org, None, allow_ambiguous=True)
        )
    except HTTPException:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_UNCONFIGURED)

    if call.transport == "browser" and sd.domain != call.metadata_.get(
        "browser_capabilities", {}
    ).get("inboxAddress"):
        return AgentSendResponse(
            ok=False,
            spoken="Your workspace email changed. Start a new conversation before sending.",
        )

    parent = None
    source_attachments = []
    if body.reply_to_email_id or body.attachment_ids:
        try:
            scope, parent, source_attachments = await source_files(
                db, call, body.reply_to_email_id, body.attachment_ids
            )
            if recipient.lower() != scope.peer.lower():
                raise ValueError("Files can only return to this conversation's person")

            if (
                sum(a.size_bytes for a in source_attachments)
                + len(body.body_text.encode())
                > MAX_EMAIL_ATTACHMENT_BYTES
            ):
                raise ValueError("Files too large")
        except ValueError:
            return AgentSendResponse(
                ok=False,
                spoken="Those files or that email are not available in this conversation. Nothing was sent.",
            )

    if call.transport == "browser" and not await reserve_browser_preview(
        db,
        call.id,
        {
            "channel": "email",
            "recipient": recipient,
            "body": body.body_text,
            "subject": body.subject,
            "preview": False,
            "tool_invocation_id": str(body.tool_invocation_id),
            "status": "queued",
        },
    ):
        return AgentSendResponse(
            ok=False,
            spoken="Your three test emails are used, or the conversation has ended. Nothing was sent.",
        )

    email = Email(
        organization_id=org,
        email_domain_id=sd.id,
        from_address=from_address_for(sd, None),
        to_addresses=[recipient],
        subject=body.subject,
        body_text=body.body_text,
        status="queued",
        provider="ses",
        metadata_={
            **_meta(body),
            **(
                {"browser_trial": True}
                if call.transport == "browser" and call.metadata_.get("browser_trial")
                else {}
            ),
        },
    )
    if parent is not None:
        email.subject = reply_subject(parent.subject or body.subject)
        email.in_reply_to, email.references_ids = reply_references(parent)
    email.metadata_ = {
        **email.metadata_,
        "source_attachment_ids": [str(a.id) for a in source_attachments],
        "attachment_reply_to_email_id": str(parent.id) if parent else None,
    }
    db.add(email)
    await db.commit()
    await db.refresh(email)

    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.email.send",
        resource_type="email",
        resource_id=email.id,
        payload={
            **_meta(body),
            "to": email.to_addresses,
            "subject": email.subject,
            "consent_source": "voice_call",
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )

    err = await deliver_email(
        db,
        email_provider,
        email,
        at_most_once=True,
        **({"attachment_rows": source_attachments} if source_attachments else {}),
        **(
            {"billable": False}
            if call.transport == "browser" and call.metadata_.get("browser_trial")
            else {}
        ),
    )
    await _record_browser_outcome(db, body.call_id, body.tool_invocation_id, email)
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.send_failed",
            resource_type="email",
            resource_id=email.id,
            payload={**_meta(body), "end_reason": err},
            actor_kind="system",
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_EMAIL_SENT)


_SPOKEN_HANDOVER_UNAVAILABLE = "I can't connect you to that person right now."
_SPOKEN_HANDOVER_DONE = "You are already connected."
# A caller ID a connected phone system passes on to the handover: +E.164 or
# an extension of 1 to 6 digits.
_PBX_CALLER_ID = re.compile(r"\+[1-9][0-9]{1,14}|[0-9]{1,6}")

# Wire id: a contact's uuid or ``member:<user uuid>`` (dispatch metadata
# ``handover_targets``).
HandoverWireId = Annotated[str, AfterValidator(normalize_contact_id)]


class AgentHandoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    contact_id: HandoverWireId


class AgentHandoverResponse(BaseModel):
    ok: bool
    spoken: str
    to_e164: str | None = None
    from_e164: str | None = None
    trunk_id: str | None = None
    headers: dict[str, str] | None = None
    # The most seconds the balance pays for once the contact answers.
    max_seconds: int | None = None


class AgentHandoverResultRequest(BaseModel):
    # Unknown fields are ignored, not refused, so a later rollback to this
    # version still records results from a newer voicebot. Versions before
    # it refuse unknown fields: roll back api and voicebot together.
    model_config = ConfigDict(extra="ignore")
    tool_invocation_id: UUID
    call_id: UUID
    contact_id: HandoverWireId
    # cancelled: the caller asked to stop while it rang.
    outcome: Literal["answered", "no_answer", "busy", "failed", "cancelled"]
    sip_status: int | None = None
    ring_ms: int = Field(ge=0)
    # Sent by voicebots from before the plain forward (their pickup check's
    # verdict). Accepted so a rolling deploy never refuses their result;
    # not stored.
    picked_up_by: str | None = Field(default=None, max_length=32)
    # What became of the text to the contact (``/handover-sms``'s ``sms``).
    sms: str | None = Field(default=None, max_length=48)
    # What became of the email to the contact (``/handover-email``).
    email: str | None = Field(default=None, max_length=48)


class AgentHandoverNoticeRequest(BaseModel):
    """The text (``/handover-sms``) or email (``/handover-email``) to the
    contact while the voicebot dials them."""

    model_config = ConfigDict(extra="forbid")
    tool_invocation_id: UUID
    call_id: UUID
    contact_id: HandoverWireId
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)


class AgentHandoverSmsResponse(BaseModel):
    # sent | failed | blocked:<why> | skipped:<why>
    sms: str


class AgentHandoverEmailResponse(BaseModel):
    # sent | failed | blocked:<why> | skipped:<why>
    email: str


# Subject of the email a handover contact gets while the agent dials them.
HANDOVER_EMAIL_SUBJECT = "{agent} is calling you to connect a caller"
HANDOVER_EMAIL_SUBJECT_NAME_CHARS = 80


async def _handover_result_event(
    db: AsyncSession, call_id: UUID, tool_invocation_id: UUID
) -> CallEvent | None:
    return (
        await db.execute(
            select(CallEvent)
            .where(
                CallEvent.call_id == call_id,
                CallEvent.kind == HANDOVER_EVENT_KIND,
                CallEvent.payload["tool_invocation_id"].astext
                == str(tool_invocation_id),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def _linked_person(
    db: AsyncSession, call: Call, wire_id: str
) -> HandoverPerson | None:
    """The contact or org member behind ``wire_id``, if the call's agent
    still links it and it still belongs to the call's org."""
    if call.agent_id is None:
        return None
    agent = await db.get(Agent, call.agent_id)
    if agent is None or agent.organization_id != call.organization_id:
        return None
    for row in (await load_handover(db, [call.agent_id])).get(call.agent_id, []):
        if row["contact_id"] == wire_id:
            return HandoverPerson(
                wire_id,
                row["name"],
                row["phone_e164"],
                row.get("email"),
                row.get("extension"),
            )
    return None


async def _link_row_exists(db: AsyncSession, call: Call, wire_id: str) -> bool:
    """True if the call's agent has a handover link row for ``wire_id``,
    even for a member who has since left the org."""
    if call.agent_id is None:
        return False
    kind, value = parse_contact_id(wire_id)
    column = (
        AgentHandoverContact.user_id
        if kind == "member"
        else AgentHandoverContact.contact_id
    )
    return (
        await db.execute(
            select(AgentHandoverContact.agent_id)
            .where(AgentHandoverContact.agent_id == call.agent_id, column == value)
            .limit(1)
        )
    ).first() is not None


@router.post("/handover", response_model=AgentHandoverResponse)
async def agent_handover(
    body: AgentHandoverRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentHandoverResponse:
    deny = AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_UNAVAILABLE)
    call = await db.get(Call, body.call_id)
    await _tag_call(db, call)
    if call is None or call.status != "in_progress":
        return deny
    if await has_answered_handover(db, call.id):
        return AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_DONE)
    if call.metadata_.get(CALL_META_BILLED) and not await has_funds(
        db, call.organization_id, "call"
    ):
        return await _deny_handover(call, body, "insufficient_funds")
    # The Hail number row: outbound dials from it, inbound rang it.
    number_id = (
        call.to_number_id if call.direction == "inbound" else call.from_number_id
    )
    number = await db.get(PhoneNumber, number_id) if number_id else None
    if number is None:
        return await _deny_handover(call, body, "carrier_route_failed")
    contact = await _linked_person(db, call, body.contact_id)
    if contact is None:
        return deny
    via_pbx = call.provider == PBX
    # On a connected call the extension wins: the phone system dials it. A
    # carrier call dials only a phone.
    extension = pbx_extension(contact.extension) if via_pbx else None
    target = extension or contact.phone_e164
    if not target:
        return deny
    other_party = _other_party(call)
    if target == other_party:
        # Dialing it rings the phone already on the call: it declines or goes
        # to voicemail, and the agent introduces the caller to the mailbox.
        return await _deny_handover(
            call,
            body,
            "contact_is_on_call",
            spoken=ON_CALL_LINE.format(name=contact.name),
        )
    if extension is None:
        # Any carrier, as on save: the second leg dials a person, not a
        # number the call's carrier sells.
        country = country_of(target)
        try:
            sold = country is not None and sells_in(country)
        except Exception:  # e.g. a missing catalog file
            sold = False
        if not sold:
            return await _deny_handover(call, body, "country_not_sold")
        gate = await check_handover_allowed(db, call.organization_id, target)
        if not gate.allowed:
            return await _deny_handover(call, body, gate.reason or "gate")
    try:
        trunk_id, headers = await voice_route(db, number)
    except Exception:  # an unset trunk, a deleted connection, a DB error
        logger.warning("handover route failed for call %s", call.id, exc_info=True)
        return await _deny_handover(call, body, "carrier_route_failed")
    hail_number = call.to_e164 if call.direction == "inbound" else call.from_e164
    # A connected call shows the person on the line to the handover contact;
    # a withheld or unusable caller ID shows the Hail number instead.
    from_e164 = hail_number
    if via_pbx and other_party and _PBX_CALLER_ID.fullmatch(other_party):
        from_e164 = other_party
    max_seconds = None
    if call.metadata_.get(CALL_META_BILLED) and quotes_enabled():
        max_seconds = await _handover_cap(db, call, target, hail_number)
        if max_seconds == 0:
            return await _deny_handover(call, body, "insufficient_funds")
    return AgentHandoverResponse(
        ok=True,
        spoken="",
        to_e164=target,
        from_e164=from_e164,
        trunk_id=trunk_id,
        headers=headers or None,
        max_seconds=max_seconds,
    )


async def _handover_cap(
    db: AsyncSession, call: Call, to_e164: str, hail_number: str | None
) -> int | None:
    """The handover's time limit from the balance (0: not one minute), and
    the call's hold grown to cover it (hailhq.core.call_allowance). The time
    already talked and the ring are priced as the call alone; the handover
    as both lines plus the agent. The hold never shrinks: if nobody answers,
    the call runs on under its own limit. When the price is unknown, None:
    the agent's own handover limit applies, nothing more is held, and the
    call's full hold counts after it ends."""
    caller = call.from_e164 if call.direction == "inbound" else hail_number
    main = {
        "direction": call.direction,
        "carrier": call.provider,
        "number_e164": call.to_e164 or "",
        "caller_e164": caller or "",
    }
    second = {
        "direction": "outbound",
        "carrier": call.provider,
        "number_e164": to_e164,
        "caller_e164": hail_number or "",
    }
    org = str(call.organization_id)
    recording = bool(call.metadata_.get("recording_enabled"))
    alone, both = await asyncio.gather(
        quote_call_price(org, [main], recording),
        quote_call_price(org, [main, second], recording),
    )
    # The agent's own handover limit: the voicebot stops the handover there.
    limit = handover_limit(
        await db.get(Agent, call.agent_id) if call.agent_id else None
    )
    if alone is None or both is None:
        # The agent's own handover limit, nothing more held: a slow price
        # check must not cut off callers who have the credit. The second
        # line's price is unknown, so the call's full hold counts after it
        # ends.
        logger.warning("handover price unknown for call %s; agent limit", call.id)
        # Under the lock, like the priced path, so a handover racing this one
        # reads this dial.
        await org_lock(db, call.organization_id)
        # The call can now run until the handover limit ends: keep its hold
        # counting until then.
        await forget_price(
            db,
            call.id,
            hold_until(
                datetime.now(timezone.utc),
                (HANDOVER_RING_MINUTES * 60) + HANDOVER_PICKUP_SECONDS + limit,
            ),
        )
        await db.commit()
        return None
    per_minute, per_call = both.cents_per_minute, both.cents_per_call
    await org_lock(db, call.organization_id)
    # Read under the lock: each earlier approved dial is counted, even one
    # whose result the voicebot has not posted yet.
    tries = await handover_dials(db, call.id)
    now = datetime.now(timezone.utc)
    since = call.answered_at or call.started_at or call.requested_at
    used = ceil(max(0.0, (now - since).total_seconds()) / 60)
    # Each earlier handover dial may have billed the second line: up to a
    # minute (it picked up and failed the pickup check) and its per-call fee.
    # Counted for every dial: Hail cannot tell which ones picked up, so it
    # holds more.
    talked = used + HANDOVER_RING_MINUTES
    # The workspace's plan pays first (hailhq.core.call_allowance): the time
    # talked and the ring at the call's own plan share, then the handover.
    plan_left = (
        await plan_minutes_left(db, call.organization_id, exclude=call.id, now=now)
        if both.plan_minutes_per_minute > 0 or alone.plan_minutes_per_minute > 0
        else Decimal(0)
    )
    talked_on_plan = (
        min(talked, int(plan_left // alone.plan_minutes_per_minute))
        if alone.plan_minutes_per_minute > 0
        else 0
    )
    plan_left -= alone.plan_minutes_per_minute * talked_on_plan
    spent = (
        alone.cents_per_minute * talked
        - alone.plan_cents_per_minute * talked_on_plan
        + (per_minute - alone.cents_per_minute + per_call - alone.cents_per_call)
        * tries
        + per_call
    )
    available = await available_cents(
        db, call.organization_id, exclude=call.id, now=now
    )
    # spent goes in as the fee, so the check rounds the same sum hold() stores.
    affordable, on_plan = plan_split(available, plan_left, both, spent)
    # Both lines run from pickup; the limit starts after the pickup check.
    seconds = min(cap_seconds(affordable, HANDOVER_PICKUP_SECONDS), limit)
    if seconds < HANDOVER_MIN_SECONDS:
        seconds = 0
    if seconds > 0:
        # Hold only the minutes the handover can use.
        minutes = paid_minutes(seconds, HANDOVER_PICKUP_SECONDS)
        cents, plan = hold_parts(both, spent, minutes, on_plan)
        await hold(
            db,
            call.id,
            cents,
            hold_until(
                now, (HANDOVER_RING_MINUTES * 60) + HANDOVER_PICKUP_SECONDS + seconds
            ),
            cents_per_minute=per_minute,
            cents_per_call=per_call,
            dial=True,
            plan_minutes=alone.plan_minutes_per_minute * talked_on_plan + plan,
        )
    await db.commit()
    return seconds


async def _deny_handover(
    call: Call,
    body: AgentHandoverRequest,
    reason: str,
    spoken: str = _SPOKEN_HANDOVER_UNAVAILABLE,
) -> AgentHandoverResponse:
    await write_audit_log(
        organization_id=call.organization_id,
        api_key_id=None,
        action="agent.handover.blocked",
        resource_type="call",
        resource_id=call.id,
        payload={"contact_id": body.contact_id, "reason": reason},
        actor_kind="system",
    )
    return AgentHandoverResponse(ok=False, spoken=spoken)


async def _notice_start(
    db: AsyncSession,
    body: AgentHandoverNoticeRequest,
    model: type[Sms | Email],
    failed_statuses: tuple[str, ...],
    noun: str,
) -> tuple[Call, HandoverPerson] | str:
    """The steps the text and the email to the contact share: the call, a
    retried post, the call's state, the person, and one notice per contact
    per call. The call and person, or the result to return now."""
    call = await _load_call_for_update(db, body.call_id)
    await _tag_call(db, call)
    if call is None:
        return "skipped:no_call"
    org = call.organization_id
    prior = await _prior_send(db, model, org, body.tool_invocation_id)
    if prior is not None:
        return "failed" if prior.status in failed_statuses else "sent"
    if call.status != "in_progress":
        return "skipped:call_ended"
    contact = await _linked_person(db, call, body.contact_id)
    if contact is None:
        return "skipped:no_contact"
    # A second dial to the same person on this call sends nothing more. Not
    # skipped once connected: a voicemail pickup connects at once, and that
    # is when the contact needs the caller's number most.
    sent = (
        await db.execute(
            select(model.id)
            .where(
                model.organization_id == org,
                model.metadata_["call_id"].astext == str(call.id),
                model.metadata_["handover_contact_id"].astext == body.contact_id,
            )
            .limit(1)
        )
    ).first()
    if sent is not None:
        return f"skipped:already_{noun}"
    return call, contact


async def _notice_blocked(
    call: Call,
    body: AgentHandoverNoticeRequest,
    channel: str,
    reason: str,
    **extra: Any,
) -> str:
    await write_audit_log(
        organization_id=call.organization_id,
        api_key_id=None,
        action=f"agent.handover.{channel}_blocked",
        resource_type="call",
        resource_id=call.id,
        payload={"contact_id": body.contact_id, "reason": reason, **extra},
        actor_kind="system",
    )
    return f"blocked:{reason}"


def _notice_meta(call: Call, body: AgentHandoverNoticeRequest) -> dict[str, str]:
    return {
        "call_id": str(call.id),
        "tool_invocation_id": str(body.tool_invocation_id),
        "handover_contact_id": body.contact_id,
    }


async def _notice_text(db: AsyncSession, call: Call, body: AgentHandoverNoticeRequest):
    """The agent's name and the words of the text and the email."""
    agent = await db.get(Agent, call.agent_id) if call.agent_id else None
    agent_name = agent.name if agent else "Your agent"
    caller = _other_party(call)
    words = handover_sms_text(
        agent_name, caller if is_e164(caller) else None, body.reason
    )
    return agent_name, words


@router.post("/handover-sms", response_model=AgentHandoverSmsResponse)
async def agent_handover_sms(
    body: AgentHandoverNoticeRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentHandoverSmsResponse:
    """Text the handover contact while the voicebot dials them, from the Hail
    number on the call. Never blocks the dial: the voicebot posts this in
    parallel and records ``sms`` on the handover event."""
    start = await _notice_start(db, body, Sms, ("failed", "undelivered"), "texted")
    if isinstance(start, str):
        return AgentHandoverSmsResponse(sms=start)
    call, contact = start
    to = contact.phone_e164
    if not to:
        return AgentHandoverSmsResponse(sms="skipped:no_contact")
    if to == _other_party(call):
        return AgentHandoverSmsResponse(sms="skipped:contact_is_on_call")
    number_id = (
        call.to_number_id if call.direction == "inbound" else call.from_number_id
    )
    from_number = await db.get(PhoneNumber, number_id) if number_id else None
    org = call.organization_id
    cannot_text = from_number is None or "sms" not in from_number.capabilities
    # A number that cannot text skips the notice, unless a trial number is
    # ready and the contact is the workspace's verified trial phone: then
    # the trial number may send it.
    if cannot_text and not await _trial_may_send(db, org, to):
        return AgentHandoverSmsResponse(sms="skipped:number_cannot_text")
    denial = await _shared_denial(db, call, "text", to)
    if denial is not None:
        return AgentHandoverSmsResponse(
            sms=await _notice_blocked(call, body, "sms", denial[1])
        )
    gate = await check_sms_allowed(db, org, to)
    if not gate.allowed:
        return AgentHandoverSmsResponse(
            sms=await _notice_blocked(
                call, body, "sms", "compliance", detail=gate.reason, checks=gate.checks
            )
        )
    cap_denial = await check_agent_send_allowed(db, org, "sms", [to])
    if cap_denial is not None:
        return AgentHandoverSmsResponse(
            sms=await _notice_blocked(
                call, body, "sms", "agent_caps", detail=cap_denial.reason
            )
        )
    blocked = (
        not cannot_text
        and await us_registration_block(db, org, from_number, to) is not None
    )
    # A blocked notice to any contact but the verified trial phone answers
    # now, before the notice text is built or a trial claimed.
    if blocked and not await _trial_may_send(db, org, to):
        return AgentHandoverSmsResponse(
            sms=await _notice_blocked(call, body, "sms", "us_registration")
        )

    _, words = await _notice_text(db, call, body)
    sms_body = words[:SMS_MAX_BODY_CHARS]
    meta = _notice_meta(call, body)
    if cannot_text or blocked:
        # The notice is cut to fit, so the footer always stays. A refused
        # claim keeps today's answer and is not rolled back: this
        # transaction may hold send-cap rows, which a refusal never commits.
        trial = await claim_trial_send(
            db, org, to=to, body=words, limit=SMS_MAX_BODY_CHARS, truncate=True
        )
        if trial is None:
            if cannot_text:
                return AgentHandoverSmsResponse(sms="skipped:number_cannot_text")
            return AgentHandoverSmsResponse(
                sms=await _notice_blocked(call, body, "sms", "us_registration")
            )
        # The claim holds the workspace lock until the commit below.
        from_number = trial.number
        sms_body = trial.body
        meta = {**meta, **trial.metadata}
    sms = Sms(
        organization_id=org,
        provider=from_number.provider,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=to,
        agent_id=call.agent_id,
        direction="outbound",
        status="queued",
        body=sms_body,
        metadata_=meta,
    )
    db.add(sms)
    await db.commit()
    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.handover.sms",
        resource_type="sms",
        resource_id=sms.id,
        payload={
            "call_id": str(call.id),
            "contact_id": body.contact_id,
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )
    err = await deliver_sms(db, sms)
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.handover.sms_failed",
            resource_type="sms",
            resource_id=sms.id,
            payload={"call_id": str(call.id), "end_reason": err},
            actor_kind="system",
        )
        return AgentHandoverSmsResponse(sms="failed")
    return AgentHandoverSmsResponse(sms="sent")


@router.post("/handover-email", response_model=AgentHandoverEmailResponse)
async def agent_handover_email(
    body: AgentHandoverNoticeRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentHandoverEmailResponse:
    """Email the handover contact while the voicebot dials them, with the
    same words as the text. Never blocks the dial: the voicebot posts this
    in parallel and records ``email`` on the handover event."""
    start = await _notice_start(db, body, Email, ("failed", "bounced"), "emailed")
    if isinstance(start, str):
        return AgentHandoverEmailResponse(email=start)
    call, contact = start
    org = call.organization_id
    to = contact.email
    if not to:
        return AgentHandoverEmailResponse(email="skipped:no_email")
    try:
        # As in /send-email: a voice agent cannot pick a sending domain.
        sd = await resolve_sender(db, org, None, allow_ambiguous=True)
    except HTTPException:
        return AgentHandoverEmailResponse(email="skipped:no_sender")
    denial = await _shared_denial(db, call, "email")
    if denial is not None:
        return AgentHandoverEmailResponse(
            email=await _notice_blocked(call, body, "email", denial[1])
        )
    gate = await check_email_allowed(db, org, [to])
    if not gate.allowed:
        return AgentHandoverEmailResponse(
            email=await _notice_blocked(
                call,
                body,
                "email",
                "compliance",
                detail=gate.reason,
                checks=gate.checks,
            )
        )
    cap_denial = await check_agent_send_allowed(
        db, org, "email", [normalize_recipient(to)]
    )
    if cap_denial is not None:
        return AgentHandoverEmailResponse(
            email=await _notice_blocked(
                call, body, "email", "agent_caps", detail=cap_denial.reason
            )
        )

    agent_name, words = await _notice_text(db, call, body)
    # The name goes in a header: one line, kept short.
    subject_name = " ".join(agent_name.split())[:HANDOVER_EMAIL_SUBJECT_NAME_CHARS]
    email = Email(
        organization_id=org,
        email_domain_id=sd.id,
        from_address=from_address_for(sd, None),
        to_addresses=[to],
        subject=HANDOVER_EMAIL_SUBJECT.format(agent=subject_name or "Your agent"),
        body_text=words,
        status="queued",
        provider="ses",
        metadata_=_notice_meta(call, body),
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)
    await write_audit_log(
        organization_id=org,
        api_key_id=None,
        action="agent.handover.email",
        resource_type="email",
        resource_id=email.id,
        payload={
            "call_id": str(call.id),
            "contact_id": body.contact_id,
            "message_type": "transactional",
            "compliance": gate.checks,
        },
        actor_kind="system",
    )
    err = await deliver_email(db, email_provider, email, at_most_once=True)
    if err is not None:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.handover.email_failed",
            resource_type="email",
            resource_id=email.id,
            payload={"call_id": str(body.call_id), "end_reason": err},
            actor_kind="system",
        )
        return AgentHandoverEmailResponse(email="failed")
    return AgentHandoverEmailResponse(email="sent")


@router.post("/handover-result")
async def agent_handover_result(
    body: AgentHandoverResultRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, bool]:
    call = await _load_call_for_update(db, body.call_id)
    await _tag_call(db, call)
    if call is None:
        return {"ok": False}
    # The dial already happened. Record it even if the person left the org
    # since: the link row still exists, only the name is gone.
    contact = await _linked_person(db, call, body.contact_id)
    if contact is None and not await _link_row_exists(db, call, body.contact_id):
        return {"ok": False}
    recorded = await _handover_result_event(db, call.id, body.tool_invocation_id)
    if recorded is not None:
        # A retry, or the voicebot's follow-up once the text and email
        # finished: only what became of them may change.
        notices = {
            k: v
            for k, v in (("sms", body.sms), ("email", body.email))
            if v not in (None, "pending") and recorded.payload.get(k) != v
        }
        if notices:
            recorded.payload = {**recorded.payload, **notices}
            await db.commit()
        return {"ok": True}
    if body.outcome == HANDOVER_ANSWERED and await has_answered_handover(db, call.id):
        return {"ok": True}  # retried result: already recorded
    name = contact.name if contact else None
    db.add(
        CallEvent(
            call_id=call.id,
            kind=HANDOVER_EVENT_KIND,
            payload={
                "tool_invocation_id": str(body.tool_invocation_id),
                "contact_id": body.contact_id,
                "name": name,
                "outcome": body.outcome,
                "sip_status": body.sip_status,
                "ring_ms": body.ring_ms,
                "sms": body.sms,
                "email": body.email,
            },
        )
    )
    if body.outcome == HANDOVER_ANSWERED:
        await fanout_call_event(
            db,
            organization_id=call.organization_id,
            event_type="call.transferred",
            event_id=call.id,
            data=call_event_data(
                call,
                transfer={"contact_id": body.contact_id, "contact_name": name},
            ),
        )
    await db.commit()
    return {"ok": True}


class AgentInboxRequest(InboxSelection):
    call_id: UUID


@router.post("/inbox")
async def agent_inbox(
    body: AgentInboxRequest, db: Annotated[AsyncSession, Depends(get_session)]
):
    call = await db.get(Call, body.call_id)
    if call is None or call.status != "in_progress":
        return {"spoken": "This conversation has ended. Inbox access is closed."}
    await _tag_call(db, call)
    return await conversation_inbox(body, db, call)


class EmailRunInboxRequest(InboxSelection):
    inbound_email_id: UUID


@router.post("/email-run/inbox")
async def email_run_inbox(
    body: EmailRunInboxRequest, db: Annotated[AsyncSession, Depends(get_session)]
):
    inbound = await db.get(Email, body.inbound_email_id)
    if inbound is None or inbound.direction != "inbound":
        return {"spoken": "This email conversation is not available."}
    await key_lock(db, email_thread_lock_key(inbound))
    await db.refresh(inbound)
    routed = await _routed_email(db, inbound)
    if (
        routed is None
        or inbound.agent_reply_state != "processing"
        or await run_refusal(db, routed[0], inbound, count_own_sends=True) is not None
    ):
        return {"spoken": "This email conversation is no longer available."}
    return await conversation_inbox(body, db, inbound)


__all__ = ["router"]
