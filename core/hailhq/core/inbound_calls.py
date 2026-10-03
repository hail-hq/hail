"""Open a ``Call`` row for an inbound SIP call.

LiveKit answers the INVITE, creates the room and dispatches the voicebot with
the dispatch rule's static metadata (``{"direction": "inbound"}``). The
voicebot reads the SIP participant's attributes and calls
:func:`open_inbound_call`, which decides who answers:

* unknown, pool, or misrouted number: dropped, no row (same as inbound SMS);
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
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.carrier_routing import carrier_for_inbound_trunk
from hailhq.core.compliance_gate import check_channel_suspended
from hailhq.core.config import settings
from hailhq.core.internal_webhook import fetch_organization_name
from hailhq.core.models import (
    Agent,
    Call,
    CallEvent,
    OrganizationCallSettings,
    PhoneNumber,
)
from hailhq.core.sms_ingest import active_dedicated_number
from hailhq.core.webhook_fanout import call_event_data, fanout_call_event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["Accepted", "Rejected", "SipAttributes", "open_inbound_call"]


@dataclass(frozen=True)
class SipAttributes:
    """What LiveKit exposes on the SIP participant at join."""

    dialed: str  # sip.trunkPhoneNumber: the org number
    caller: str  # sip.phoneNumber
    trunk_id: str  # sip.trunkID
    room_name: str
    provider_call_sid: str | None  # sip.callIDFull


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
    try:
        trunk_carrier = carrier_for_inbound_trunk(attrs.trunk_id)
    except ValueError:
        trunk_carrier = None
    if trunk_carrier != number.provider or "voice" not in number.capabilities:
        logger.info(
            "inbound call to %s dropped: trunk carrier=%s number carrier=%s "
            "capabilities=%s",
            attrs.dialed,
            trunk_carrier,
            number.provider,
            number.capabilities,
        )
        return Rejected("wrong_carrier")

    # The caller is already connected and hearing silence: look the name up
    # while the checks and the Call row are written, as POST /calls does.
    org_id = number.organization_id
    org_name = asyncio.create_task(fetch_organization_name(str(org_id)))
    try:
        return await _answer_or_refuse(db, number, org_id, attrs, org_name)
    finally:
        org_name.cancel()  # no-op once it has finished (refusals never await it)


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
    if agent is None or agent.status != "live":
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
        metadata_={CALL_META_BILLED: True},
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
            "max_duration_seconds": max_duration,
            "voice_config": dict(agent.voice_config),
            "system_prompt": agent.system_prompt,
            "llm": None,
            "first_message": agent.first_message,
            "ai_disclosure": agent.ai_disclosure,
            "ai_disclosure_line": disclosure_line,
            "tools": agent.tools,
            "org_name": await org_name,
        }
    )
