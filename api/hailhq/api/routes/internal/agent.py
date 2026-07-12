"""Voicebot → API agent-send routes.

The voice agent's send tools execute here so the full existing outbound
stack — suppression/velocity gate, funds, audit, disclosure footer,
billing — runs unchanged (spec: docs/superpowers/specs/
2026-07-11-voicebot-agent-tools-design.md). Auth is the shared
HAIL_INTERNAL_SECRET HMAC (routes/internal/auth.py).

Responses are always HTTP 200 with ``{ok, spoken}`` — ``spoken`` is a
short plain sentence the agent says on the call. Policy denials are
data, not HTTP errors, and stay deliberately vague: never reveal
suppression-list membership or a member's address to the callee.

The agent never supplies addresses: SMS always targets the call's
counterpart (``calls.to_e164``); email targets a directory name resolved
here, scoped to the call's org.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.numbers import resolve_org_number
from hailhq.api.routes.email_domains import get_email_provider
from hailhq.api.routes.emails import deliver_email, from_address_for, resolve_sender
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.sms import deliver_sms, get_sms_provider
from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.compliance_gate import check_email_allowed, check_sms_allowed
from hailhq.core.db import get_session
from hailhq.core.directory import resolve_member_emails
from hailhq.core.models import Call, Email, Sms
from hailhq.core.providers.email import EmailProvider
from hailhq.core.providers.sms import SmsProvider

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
    body: str = Field(min_length=1, max_length=480)


class AgentSendEmailRequest(AgentSendBase):
    recipient_name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    body_text: str = Field(min_length=1, max_length=5000)


class AgentSendResponse(BaseModel):
    ok: bool
    spoken: str


def _meta(req: AgentSendBase) -> dict[str, str]:
    return {
        "call_id": str(req.call_id),
        "tool_invocation_id": str(req.tool_invocation_id),
    }


async def _load_live_call(db: AsyncSession, call_id: UUID) -> Call | None:
    # FOR UPDATE on the call row serializes concurrent agent sends for one
    # call: the dedupe SELECT and the cap COUNT both run under this lock, so
    # a timeout-retry racing the original can't double-send or blow past
    # AGENT_SEND_CAP. The commit that releases the lock also publishes the
    # row the next request's dedupe will see; never hold the lock across
    # deliver_* — the Sms/Email INSERT + commit happens before the slow
    # provider send.
    call = (
        await db.execute(select(Call).where(Call.id == call_id).with_for_update())
    ).scalar_one_or_none()
    if call is None or call.status != "in_progress":
        return None
    return call


async def _sends_this_call(db: AsyncSession, org_id: UUID, call_id: UUID) -> int:
    key = str(call_id)
    emails = (
        await db.execute(
            select(func.count())
            .select_from(Email)
            .where(
                Email.organization_id == org_id,
                Email.metadata_["call_id"].astext == key,
            )
        )
    ).scalar_one()
    sms = (
        await db.execute(
            select(func.count())
            .select_from(Sms)
            .where(
                Sms.organization_id == org_id,
                Sms.metadata_["call_id"].astext == key,
            )
        )
    ).scalar_one()
    return int(emails) + int(sms)


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
    provider: Annotated[SmsProvider, Depends(get_sms_provider)],
) -> AgentSendResponse:
    call = await _load_live_call(db, body.call_id)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    # Idempotent replay: the voicebot retries timeouts with the same id.
    prior = (
        await db.execute(
            select(Sms).where(
                Sms.organization_id == org,
                Sms.metadata_["tool_invocation_id"].astext
                == str(body.tool_invocation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        # queued = committed-but-in-flight (the concurrent original holds
        # it between commit and delivery reconciliation); optimistic
        # success avoids the duplicate-send failure mode, which is the
        # worse error on a live call.
        ok = prior.status not in ("failed", "undelivered")
        return AgentSendResponse(
            ok=ok, spoken=_SPOKEN_SMS_SENT if ok else _SPOKEN_SMS_FAILED
        )

    denial = await _shared_denial(db, call)
    if denial is not None:
        spoken, reason = denial
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={**_meta(body), "reason": reason},
        )
        return AgentSendResponse(ok=False, spoken=spoken)

    gate = await check_sms_allowed(db, org, call.to_e164)
    if not gate.allowed:
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.sms.blocked",
            resource_type="sms",
            resource_id=None,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_NOT_ALLOWED)

    from_number = await resolve_org_number(db, org, None, capability="sms")
    if from_number is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_UNCONFIGURED)

    sms = Sms(
        organization_id=org,
        from_number_id=from_number.id,
        from_e164=from_number.e164,
        to_e164=call.to_e164,  # counterpart only — never a parameter
        direction="outbound",
        status="queued",
        body=body.body,
        metadata_=_meta(body),
    )
    db.add(sms)
    await db.commit()

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
    )

    err = await deliver_sms(db, provider, sms)
    if err is not None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_SMS_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_SMS_SENT)


@router.post("/send-email", response_model=AgentSendResponse)
async def agent_send_email(
    body: AgentSendEmailRequest,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> AgentSendResponse:
    call = await _load_live_call(db, body.call_id)
    if call is None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_CALL_UNAVAILABLE)
    org = call.organization_id

    prior = (
        await db.execute(
            select(Email).where(
                Email.organization_id == org,
                Email.metadata_["tool_invocation_id"].astext
                == str(body.tool_invocation_id),
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        # Not just "sent": "delivered" is reachable via the SES delivery
        # webhook (core/hailhq/core/email_delivery_events.py), and
        # queued = committed-but-in-flight (the concurrent original holds
        # it between commit and delivery reconciliation). Optimistic
        # success avoids the duplicate-send failure mode, which is the
        # worse error on a live call.
        ok = prior.status != "failed"
        return AgentSendResponse(
            ok=ok, spoken=_SPOKEN_EMAIL_SENT if ok else _SPOKEN_EMAIL_FAILED
        )

    denial = await _shared_denial(db, call)
    if denial is not None:
        spoken, reason = denial
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.blocked",
            resource_type="email",
            resource_id=None,
            payload={**_meta(body), "reason": reason},
        )
        return AgentSendResponse(ok=False, spoken=spoken)

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
        await write_audit_log(
            organization_id=org,
            api_key_id=None,
            action="agent.email.blocked",
            resource_type="email",
            resource_id=None,
            payload={**_meta(body), "reason": gate.reason, "checks": gate.checks},
        )
        return AgentSendResponse(ok=False, spoken=_SPOKEN_NOT_ALLOWED)

    try:
        sd = await resolve_sender(db, org, None)
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
    )

    err = await deliver_email(db, email_provider, email)
    if err is not None:
        return AgentSendResponse(ok=False, spoken=_SPOKEN_EMAIL_FAILED)
    return AgentSendResponse(ok=True, spoken=_SPOKEN_EMAIL_SENT)


__all__ = ["router", "AGENT_SEND_CAP"]
