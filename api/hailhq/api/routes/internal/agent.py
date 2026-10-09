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
email targets a directory name resolved
here, scoped to the call's org.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from hailhq.api.audit import write_audit_log
from hailhq.api.numbers import resolve_sms_number
from hailhq.api.routes.email_domains import get_email_provider
from hailhq.api.routes.emails import deliver_email, resolve_sender
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.sms import deliver_sms
from hailhq.core.agent_caps import check_agent_send_allowed
from hailhq.core.agent_tools.send_email import (
    MAX_BODY_CHARS as EMAIL_MAX_BODY_CHARS,
)
from hailhq.core.agent_tools.send_email import (
    MAX_RECIPIENT_NAME_CHARS,
    MAX_SUBJECT_CHARS,
)
from hailhq.core.agent_tools.send_sms import MAX_BODY_CHARS as SMS_MAX_BODY_CHARS
from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.carrier_routing import voice_route
from hailhq.core.compliance_gate import (
    check_email_allowed,
    check_handover_allowed,
    check_sms_allowed,
    normalize_recipient,
)
from hailhq.core.contact_ids import normalize_contact_id
from hailhq.core.db import get_session
from hailhq.core.directory import resolve_member_emails
from hailhq.core.email_sender import from_address_for
from hailhq.core.handover import (
    HANDOVER_ANSWERED,
    HANDOVER_EVENT_KIND,
    HandoverPerson,
    country_of,
    has_answered_handover,
    load_handover,
)
from hailhq.core.models import (
    Agent,
    Call,
    CallEvent,
    Email,
    PhoneNumber,
    Sms,
)
from hailhq.core.providers.email import EmailProvider
from hailhq.core.telephony_catalog import sells_in
from hailhq.core.text_agent import (
    MAX_REPLIES_PER_THREAD,
    answers_texts,
    replies_in_thread,
    thread_lock_key,
)
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/internal/agent",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)

AGENT_SEND_CAP = 5  # total agent-initiated sends (sms + email) per call

_SPOKEN_CALL_UNAVAILABLE = "This call can no longer send messages."
_SPOKEN_NOT_ALLOWED = "I'm not able to send that message."
_SPOKEN_CAP = "I've reached the limit of messages I can send on this call."
_SPOKEN_SMS_SENT = "Text message sent to the number on this call."
_SPOKEN_SMS_FAILED = "I couldn't send the text message."
_SPOKEN_SMS_UNCONFIGURED = "Text messaging isn't set up for this account."
_SPOKEN_EMAIL_SENT = "Email sent."
_SPOKEN_EMAIL_FAILED = "I couldn't send the email."
_SPOKEN_EMAIL_UNCONFIGURED = "Email isn't set up for this account."


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
    # call: the dedupe SELECT and the cap COUNT both run under this lock, so
    # a timeout-retry racing the original can't double-send or blow past
    # AGENT_SEND_CAP. The commit that releases the lock also publishes the
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
    return (
        await db.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()


async def _sends_this_call(db: AsyncSession, org_id: UUID, call_id: UUID) -> int:
    key = str(call_id)
    email_count = (
        select(func.count())
        .select_from(Email)
        .where(
            Email.organization_id == org_id,
            Email.metadata_["call_id"].astext == key,
        )
        .scalar_subquery()
    )
    sms_count = (
        select(func.count())
        .select_from(Sms)
        .where(
            Sms.organization_id == org_id,
            Sms.metadata_["call_id"].astext == key,
        )
        .scalar_subquery()
    )
    # One round trip: two scalar subqueries summed in a single SELECT,
    # instead of two sequential COUNT queries.
    total = (await db.execute(select(email_count + sms_count))).scalar_one()
    return int(total)


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


async def _shared_denial(db: AsyncSession, call: Call) -> tuple[str, str] | None:
    """Cap + funds checks shared by both send routes.

    Returns ``(spoken, audit_reason)`` on denial or None when the send may
    proceed. The audit reason distinguishes which gate fired; the spoken
    text stays vague for the callee.
    """
    if await _sends_this_call(db, call.organization_id, call.id) >= AGENT_SEND_CAP:
        return _SPOKEN_CAP, "send_cap"
    if call.metadata_.get(CALL_META_BILLED) and not await has_funds(
        db, call.organization_id
    ):
        return _SPOKEN_NOT_ALLOWED, "insufficient_funds"
    return None


@router.post("/send-sms", response_model=AgentSendResponse)
async def agent_send_sms(
    body: AgentSendSmsRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentSendResponse:
    call = await _load_call_for_update(db, body.call_id)
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
            ok=ok, spoken=_SPOKEN_SMS_SENT if ok else _SPOKEN_SMS_FAILED
        )

    if call.status != "in_progress":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)

    # The person on the line: the callee on outbound, the caller on inbound.
    counterpart = call.from_e164 if call.direction == "inbound" else call.to_e164

    denial = await _shared_denial(db, call)
    if denial is not None:
        spoken, reason = denial
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=spoken,
            payload={**_meta(body), "reason": reason},
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

    # Platform agent-caps gate (velocity + kill switch): voicebot sends must
    # count toward the same per-recipient caps the public routes enforce —
    # no-op for human-origin orgs. Same recipient set as the gate above.
    cap_denial = await check_agent_send_allowed(db, org, "sms", [counterpart])
    if cap_denial is not None:
        return await _deny(
            org,
            action="agent.sms.blocked",
            resource_type="sms",
            spoken=_SPOKEN_NOT_ALLOWED,
            payload={
                **_meta(body),
                "reason": "agent_caps",
                "detail": cap_denial.reason,
            },
        )

    # Text from the number the caller dialed when it can text; else a text
    # number of this agent (see resolve_sms_number).
    dialed = (
        await db.get(PhoneNumber, call.to_number_id)
        if call.direction == "inbound" and call.to_number_id is not None
        else None
    )
    from_number = await resolve_sms_number(db, org, call.agent_id, dialed)
    if from_number is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)

    auto_bound = db.info.pop("auto_bound_sms_number_id", None) == from_number.id

    sms = Sms(
        organization_id=org,
        provider=from_number.provider,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=counterpart,  # the person on the line — never a parameter
        agent_id=call.agent_id,
        direction="outbound",
        status="queued",
        body=body.body,
        metadata_=_meta(body),
    )
    db.add(sms)
    await db.commit()

    if auto_bound:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="number.route",
            resource_type="phone_number",
            resource_id=from_number.id,
            payload={
                "e164": from_number.e164,
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

    err = await deliver_sms(db, sms)
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
    reason: str | None = None
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
        return AgentReplySmsResponse(ok=False, state="skipped", reason="no_agent")

    # One request per thread at a time: the lock is held until the reply row
    # is committed, so a retry that races the original sees it below and the
    # per-thread reply cap below is exact. A lock on the inbound row (FOR
    # UPDATE) would deadlock with the worker's claim, hence an advisory lock
    # keyed by thread.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": thread_lock_key(inbound)},
    )
    # Idempotent: the worker retries a timed-out POST with the same sms_id.
    # ``first()`` stays as a belt for rows written before the lock existed.
    prior = (
        (
            await db.execute(
                select(Sms)
                .where(
                    Sms.organization_id == org,
                    Sms.direction == "outbound",
                    Sms.agent_id.is_not(None),
                    Sms.to_e164 == inbound.from_e164,
                    Sms.metadata_["reply_to_sms_id"].astext == str(inbound.id),
                )
                .order_by(Sms.requested_at)
            )
        )
        .scalars()
        .first()
    )
    if prior is not None:
        return AgentReplySmsResponse(ok=True, state="done", reply_id=prior.id)

    if await replies_in_thread(db, inbound) >= MAX_REPLIES_PER_THREAD:
        return AgentReplySmsResponse(ok=False, state="skipped", reason="thread_cap")
    if not await has_funds(db, org):
        return AgentReplySmsResponse(
            ok=False, state="skipped", reason="insufficient_funds"
        )
    gate = await check_sms_allowed(db, org, inbound.from_e164)
    if not gate.allowed:
        return AgentReplySmsResponse(ok=False, state="skipped", reason=gate.reason)
    cap_denial = await check_agent_send_allowed(db, org, "sms", [inbound.from_e164])
    if cap_denial is not None:
        return AgentReplySmsResponse(ok=False, state="skipped", reason="agent_caps")

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
        return AgentReplySmsResponse(
            ok=False, state="failed", reason=err, reply_id=reply.id
        )
    return AgentReplySmsResponse(ok=True, state="done", reply_id=reply.id)


@router.post("/send-email", response_model=AgentSendResponse)
async def agent_send_email(
    body: AgentSendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentSendResponse:
    call = await _load_call_for_update(db, body.call_id)
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
            ok=ok, spoken=_SPOKEN_EMAIL_SENT if ok else _SPOKEN_EMAIL_FAILED
        )

    if call.status != "in_progress":
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)

    denial = await _shared_denial(db, call)
    if denial is not None:
        spoken, reason = denial
        return await _deny(
            org,
            action="agent.email.blocked",
            resource_type="email",
            spoken=spoken,
            payload={**_meta(body), "reason": reason},
        )

    matches = await resolve_member_emails(db, org, body.recipient_name)
    if not matches:
        return AgentSendResponse(
            ok=False,
            spoken=f"I couldn't find {body.recipient_name} in the directory.",
        )
    if len(matches) > 1:
        return AgentSendResponse(
            ok=False,
            spoken=(
                f"More than one person is named {body.recipient_name}, so I "
                "can't pick a recipient."
            ),
        )
    recipient = matches[0]

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
        # A voice agent cannot name a sending domain mid-call, so this path
        # keeps the old "oldest verified wins" pick that POST /emails now
        # refuses. Public callers get the 422 and choose for themselves.
        sd = await resolve_sender(db, org, None, allow_ambiguous=True)
    except HTTPException:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_UNCONFIGURED)

    email = Email(
        organization_id=org,
        email_domain_id=sd.id,
        from_address=from_address_for(sd, None),
        to_addresses=[recipient],
        subject=body.subject,
        body_text=body.body_text,
        status="queued",
        provider="ses",
        metadata_=_meta(body),
    )
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

    err = await deliver_email(db, email_provider, email)
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


class AgentHandoverResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_invocation_id: UUID
    call_id: UUID
    contact_id: HandoverWireId
    outcome: Literal["answered", "no_answer", "busy", "failed"]
    sip_status: int | None = None
    ring_ms: int = Field(ge=0)


async def _handover_result_recorded(
    db: AsyncSession, call_id: UUID, tool_invocation_id: UUID
) -> bool:
    return (
        await db.execute(
            select(CallEvent.id)
            .where(
                CallEvent.call_id == call_id,
                CallEvent.kind == HANDOVER_EVENT_KIND,
                CallEvent.payload["tool_invocation_id"].astext
                == str(tool_invocation_id),
            )
            .limit(1)
        )
    ).first() is not None


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
            return HandoverPerson(wire_id, row["name"], row["phone_e164"])
    return None


@router.post("/handover", response_model=AgentHandoverResponse)
async def agent_handover(
    body: AgentHandoverRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> AgentHandoverResponse:
    deny = AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_UNAVAILABLE)
    call = await db.get(Call, body.call_id)
    if call is None or call.status != "in_progress":
        return deny
    if await has_answered_handover(db, call.id):
        return AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_DONE)
    if call.metadata_.get(CALL_META_BILLED) and not await has_funds(
        db, call.organization_id
    ):
        return await _deny_handover(call, body, "insufficient_funds")
    contact = await _linked_person(db, call, body.contact_id)
    if contact is None or not contact.phone_e164:
        return deny
    country = country_of(contact.phone_e164)
    try:
        sold = country is not None and sells_in(country, call.provider)
    except Exception:  # e.g. provider without a catalog file
        sold = False
    if not sold:
        return await _deny_handover(call, body, "country_not_sold")
    gate = await check_handover_allowed(db, call.organization_id, contact.phone_e164)
    if not gate.allowed:
        return await _deny_handover(call, body, gate.reason or "gate")
    try:
        trunk_id, headers = voice_route(call.provider)
    except Exception:
        return await _deny_handover(call, body, "carrier_route_failed")
    # The Hail number on this call: outbound dials from it, inbound rang it.
    hail_number = call.to_e164 if call.direction == "inbound" else call.from_e164
    return AgentHandoverResponse(
        ok=True,
        spoken="",
        to_e164=contact.phone_e164,
        from_e164=hail_number,
        trunk_id=trunk_id,
        headers=headers or None,
    )


async def _deny_handover(
    call: Call, body: AgentHandoverRequest, reason: str
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
    return AgentHandoverResponse(ok=False, spoken=_SPOKEN_HANDOVER_UNAVAILABLE)


@router.post("/handover-result")
async def agent_handover_result(
    body: AgentHandoverResultRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, bool]:
    call = await _load_call_for_update(db, body.call_id)
    if call is None:
        return {"ok": False}
    contact = await _linked_person(db, call, body.contact_id)
    if contact is None:
        return {"ok": False}
    if body.outcome == HANDOVER_ANSWERED and await has_answered_handover(db, call.id):
        return {"ok": True}  # retried result: already recorded
    if await _handover_result_recorded(db, call.id, body.tool_invocation_id):
        return {"ok": True}
    name = contact.name
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


__all__ = ["AGENT_SEND_CAP", "router"]
