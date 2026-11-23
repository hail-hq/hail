"""Open a ``Call`` row for an inbound SIP call.

LiveKit answers the INVITE, creates the room and dispatches the voicebot with
the dispatch rule's static metadata (``{"direction": "inbound"}``). The
voicebot reads the SIP participant's attributes and calls
:func:`open_inbound_call`, which decides who answers:

* unknown or pool number: dropped, no row (same as inbound SMS);
* a Twilio call without the shared secret (``HAIL_SIP_INBOUND_SECRET``) or
  with a wrong one, on any trunk: dropped, no row and no webhook (logged at
  WARNING), so a forged call never fills a customer's call log. Checked
  before the trunk: a real call on the shared trunk carries no secret
  either, so it cannot be told from a forged one (only a hint to move the
  number). A malformed secret, or no Twilio trunk of its own, turns the
  check off with an ERROR log: it never stops a service;
* known number on the wrong trunk, or without voice: ``Call`` failed with
  ``end_reason = carrier_route_failed`` (logged at WARNING);
* no live voice agent: ``Call`` failed with ``end_reason = no_agent``;
* no credits or voice suspended: ``Call`` failed with ``insufficient_funds``
  (or ``user_rejected`` when the channel is suspended);
* otherwise a ``ringing`` ``Call`` plus ``call.received``, and the metadata
  the rest of the voicebot entrypoint already understands.

Mirrors ``sms_ingest.ingest_inbound_sms`` for the resolution rules and
``api/routes/calls.create_call`` for the dispatch metadata shape.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.carrier_routing import inbound_trunk
from hailhq.core.compliance_gate import check_channel_suspended
from hailhq.core.config import SELF_HOSTED_ORG_ID, SIP_SECRET_PATTERN, settings
from hailhq.core.db import session_scope
from hailhq.core.handover import handover_limit, handover_targets
from hailhq.core.internal_webhook import fetch_organization_name
from hailhq.core.models import (
    Agent,
    Call,
    CallEvent,
    Organization,
    OrganizationCallSettings,
    PhoneNumber,
)
from hailhq.core.recordings import recording_call_metadata
from hailhq.core.sms_ingest import active_dedicated_number
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "SIP_SECRET_ATTRIBUTE",
    "SIP_SECRET_HEADER",
    "Accepted",
    "Rejected",
    "SipAttributes",
    "open_inbound_call",
]

# Twilio sends HAIL_SIP_INBOUND_SECRET in this header (set in the Elastic SIP
# trunk's origination URI); the LiveKit Twilio trunk maps it to this
# participant attribute (headers_to_attributes in
# infra/livekit/inbound-trunks.json). Both set by scripts/ops/sip_trunks.py.
SIP_SECRET_HEADER = "X-Hail-Secret"
SIP_SECRET_ATTRIBUTE = "hail.sip_secret"
# Carriers whose calls must carry the secret. Telnyx has no documented way to
# add a fixed header to its inbound INVITEs; DIDWW uses digest auth.
SIP_SECRET_CARRIERS = frozenset({"twilio"})
# Once per process: the ERROR that the check is off, and the hint to move a
# number off the shared trunk (by number).
_secret_off_logged = False
_shared_hint_logged: set[str] = set()


@dataclass(frozen=True)
class SipAttributes:
    """What LiveKit exposes on the SIP participant at join."""

    dialed: str  # sip.trunkPhoneNumber: the org number
    caller: str  # sip.phoneNumber
    trunk_id: str  # sip.trunkID
    room_name: str
    provider_call_sid: str | None  # sip.callIDFull
    # The SIP_SECRET_ATTRIBUTE attribute. Never printed.
    sip_secret: str = field(default="", repr=False)


@dataclass(frozen=True)
class Accepted:
    """Answer the call. ``metadata`` has the dispatch shape ``parse_metadata``
    returns for outbound calls, plus ``direction`` and ``ai_disclosure_line``."""

    metadata: dict[str, Any]


@dataclass(frozen=True)
class Rejected:
    """Hang up without speaking. ``call_id`` is set when a failed row was
    written (known number, refused call)."""

    reason: str
    call_id: UUID | None = None


async def _refuse(
    db: AsyncSession,
    number: PhoneNumber,
    attrs: SipAttributes,
    agent: Agent | None,
    end_reason: CallEndReason,
) -> Rejected:
    now = datetime.now(timezone.utc)
    call = Call(
        organization_id=number.organization_id,
        to_number_id=number.id,
        agent_id=agent.id if agent else None,
        from_e164=attrs.caller,
        to_e164=attrs.dialed,
        direction="inbound",
        status="failed",
        end_reason=end_reason.value,
        provider=number.provider,
        provider_call_sid=attrs.provider_call_sid,
        livekit_room=attrs.room_name,
        voice_config={},
        started_at=now,
        ended_at=now,
        metadata_={CALL_META_BILLED: True},
    )
    db.add(call)
    await db.flush()
    db.add(
        CallEvent(
            call_id=call.id,
            kind="state_change",
            payload={"from": "queued", "to": "failed", "reason": end_reason.value},
        )
    )
    await fanout_call_event(
        db,
        organization_id=number.organization_id,
        event_type="call.failed",
        event_id=call.id,
        data=call_event_data(call),
    )
    await db.commit()
    logger.info(
        "inbound call to %s from %s refused: %s (call_id=%s)",
        attrs.dialed,
        attrs.caller,
        end_reason.value,
        call.id,
    )
    return Rejected(end_reason.value, call.id)


async def open_inbound_call(
    db: AsyncSession, attrs: SipAttributes
) -> Accepted | Rejected:
    """Decide who answers and write the ``Call`` row. Commits."""
    number = await active_dedicated_number(db, attrs.dialed)
    if number is None or number.organization_id is None:
        logger.info("inbound call to unknown/pool number %s dropped", attrs.dialed)
        return Rejected("unknown_number")
    if not _secret_ok(number.provider, attrs):
        # Maybe forged, on any trunk: no Call row, no event, no webhook. A
        # failed row would let a forger write calls (any caller ID) into a
        # customer's log. Only after a valid secret does the trunk check
        # below keep a visible failed row for real misrouting.
        return Rejected("bad_secret")
    # The INVITE must arrive on the LiveKit inbound trunk configured for the
    # number's carrier: its own trunk when its key is set, else the shared
    # one. While the shared trunk exists it is accepted too, so numbers can
    # move to their carrier's trunk with no refused call (LiveKit lists a
    # number on one trunk only). Each trunk keeps a placeholder entry,
    # because a trunk with no numbers matches every dialed number.
    try:
        expected_trunk = inbound_trunk(number.provider)
    except ValueError:
        expected_trunk = None
    shared = settings.livekit_sip_inbound_trunk_id
    # The shared trunk counts only for a carrier Hail can route.
    accepted = {expected_trunk, shared} - {None, ""} if expected_trunk else set()
    if shared and attrs.trunk_id == shared and expected_trunk not in (None, shared):
        # Expected only for a number not moved yet. After the move, a call
        # here means the shared trunk matched a moved number: check that it
        # still lists its placeholder (sip_trunks.py status).
        logger.info(
            "inbound call to %s answered on the shared trunk; its carrier "
            "%s has its own trunk",
            attrs.dialed,
            number.provider,
        )
    if (
        not attrs.trunk_id
        or attrs.trunk_id not in accepted
        or "voice" not in number.capabilities
    ):
        # A real number of a real customer: keep a failed Call (and its event
        # and webhook) so the customer sees the missed call.
        logger.warning(
            "inbound call to %s refused: trunk=%s expected=%s carrier=%s "
            "capabilities=%s",
            attrs.dialed,
            attrs.trunk_id,
            expected_trunk,
            number.provider,
            number.capabilities,
        )
        return await _refuse(
            db, number, attrs, None, CallEndReason.CARRIER_ROUTE_FAILED
        )

    # The caller is already connected and hearing silence: look the name up
    # while the checks and the Call row are written, as POST /calls does.
    org_id = number.organization_id
    org_name = asyncio.create_task(_organization_name(org_id))
    try:
        return await _answer_or_refuse(db, number, org_id, attrs, org_name)
    finally:
        org_name.cancel()  # no-op once it has finished (refusals never await it)


def _secret_ok(carrier: str | None, attrs: SipAttributes) -> bool:
    """True unless the carrier must send the shared secret and did not.

    Anyone can send an INVITE to the LiveKit SIP host. LiveKit Cloud checks a
    trunk's allowed addresses only once that check is enabled for the
    project, and Twilio cannot answer a digest challenge, so Twilio sends a
    fixed header instead (docs/operations/inbound-trunk-lockdown.md). Logs
    never hold the secret.
    """
    if carrier not in SIP_SECRET_CARRIERS:
        return True
    sent = attrs.sip_secret
    # Logged with the setting empty too: the rollout's test call checks that
    # the header arrives before Hail starts to refuse calls without it.
    logger.info(
        "inbound %s call to %s: %s header %s",
        carrier,
        attrs.dialed,
        SIP_SECRET_HEADER,
        "present" if sent else "missing",
    )
    secret = _active_secret()
    if not secret:
        return True
    # Bytes: compare_digest raises TypeError on a non-ASCII str.
    if sent and hmac.compare_digest(sent.encode(), secret.encode()):
        return True
    logger.warning(
        "inbound call to %s refused: %s header %s (carrier=%s)",
        attrs.dialed,
        SIP_SECRET_HEADER,
        "wrong" if sent else "missing",
        carrier,
    )
    shared = settings.livekit_sip_inbound_trunk_id
    if shared and attrs.trunk_id == shared and attrs.dialed not in _shared_hint_logged:
        # Only the Twilio trunk maps the header: every call here is refused.
        _shared_hint_logged.add(attrs.dialed)
        logger.info(
            "inbound call to %s: Twilio number still on the shared trunk; move "
            "it (scripts/ops/sip_trunks.py move --carrier twilio)",
            attrs.dialed,
        )
    return False


def _active_secret() -> str:
    """HAIL_SIP_INBOUND_SECRET, or "" (check off) when it cannot work: a
    value unfit for a SIP URI, or no Twilio trunk of its own (only that
    trunk maps the header, so every Twilio call would be refused). Logs an
    ERROR once per process instead of stopping a service. Never logs the
    value."""
    global _secret_off_logged
    secret = settings.hail_sip_inbound_secret
    if not secret:
        return ""
    why = ""
    if not SIP_SECRET_PATTERN.fullmatch(secret):
        why = "is not 20 or more letters, digits, - or _"
    else:
        twilio_trunk = settings.livekit_twilio_sip_inbound_trunk_id
        if not twilio_trunk or twilio_trunk == settings.livekit_sip_inbound_trunk_id:
            why = "is set but no Twilio inbound trunk"
    if not why:
        return secret
    if not _secret_off_logged:
        _secret_off_logged = True
        logger.error("HAIL_SIP_INBOUND_SECRET %s: secret check off", why)
    return ""


async def _organization_name(org_id: UUID) -> str | None:
    """The org's display name from the mirrored ``organizations`` table, in
    its own session so it runs beside the answer checks. Each inbound call is
    a fresh voicebot process, so the hail.so lookup's cache never helps here;
    it is only the fallback for a missing table (self-host) or a blank name."""
    try:
        async with session_scope() as session:
            name = await session.scalar(
                select(Organization.name).where(Organization.id == org_id)
            )
    except Exception:  # a DB connect error is an OSError, not a SQLAlchemyError
        logger.warning("organizations lookup failed for %s", org_id, exc_info=True)
        name = None
    if name and name.strip():
        return name.strip()
    return await fetch_organization_name(str(org_id))


async def _answer_or_refuse(
    db: AsyncSession,
    number: PhoneNumber,
    org_id: UUID,
    attrs: SipAttributes,
    org_name: asyncio.Task[str | None],
) -> Accepted | Rejected:
    agent = (
        await db.get(Agent, number.voice_agent_id) if number.voice_agent_id else None
    )
    if agent is None or agent.status != "live" or not agent.voice_enabled:
        return await _refuse(db, number, attrs, agent, CallEndReason.NO_AGENT)
    if await check_channel_suspended(db, org_id, "voice"):
        return await _refuse(db, number, attrs, agent, CallEndReason.USER_REJECTED)
    if not await has_funds(db, org_id):
        return await _refuse(db, number, attrs, agent, CallEndReason.INSUFFICIENT_FUNDS)

    workspace = await db.get(OrganizationCallSettings, org_id)
    max_duration = (
        agent.max_duration_seconds
        or (workspace.max_duration_seconds if workspace else None)
        or settings.hail_voice_max_duration_seconds
    )
    disclosure_line = agent.ai_disclosure_line or (
        workspace.ai_disclosure_line if workspace else None
    )
    now = datetime.now(timezone.utc)
    call = Call(
        organization_id=org_id,
        to_number_id=number.id,
        agent_id=agent.id,
        from_e164=attrs.caller,
        to_e164=attrs.dialed,
        direction="inbound",
        status="ringing",
        provider=number.provider,
        provider_call_sid=attrs.provider_call_sid,
        livekit_room=attrs.room_name,
        voice_config=dict(agent.voice_config),
        max_duration_seconds=max_duration,
        initial_prompt=agent.system_prompt,
        started_at=now,
        metadata_={
            CALL_META_BILLED: True,
            **recording_call_metadata(
                workspace,
                override=agent.recording_enabled,
                # Same rule as outbound: only the shared-key sentinel org is
                # self-hosted, never every org on a deploy that sets the key.
                self_hosted=org_id == SELF_HOSTED_ORG_ID,
            ),
        },
    )
    db.add(call)
    await db.flush()
    db.add(
        CallEvent(
            call_id=call.id,
            kind="state_change",
            payload={"from": "queued", "to": "ringing"},
        )
    )
    await fanout_call_event(
        db,
        organization_id=org_id,
        event_type="call.received",
        event_id=call.id,
        data=call_event_data(call),
    )
    await db.commit()
    logger.info(
        "inbound call_id=%s to %s from %s answered by agent %s",
        call.id,
        attrs.dialed,
        attrs.caller,
        agent.id,
    )
    return Accepted(
        {
            "call_id": call.id,
            "direction": "inbound",
            "organization_id": str(org_id),
            "agent_id": str(agent.id),
            "from_e164": attrs.caller,
            "to_e164": attrs.dialed,
            "max_duration_seconds": max_duration,
            "voice_config": dict(agent.voice_config),
            "system_prompt": agent.system_prompt,
            "llm": None,
            "first_message": agent.first_message,
            "ai_disclosure": agent.ai_disclosure,
            "ai_disclosure_line": disclosure_line,
            "tools": agent.tools,
            "handover_targets": await handover_targets(db, agent.id, attrs.caller),
            "handover_max_duration_seconds": handover_limit(agent),
            "org_name": await org_name,
        }
    )
