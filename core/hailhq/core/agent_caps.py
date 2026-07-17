"""Velocity caps + kill switch for agent-origin orgs (agent self-signup v1).

Sits next to compliance_gate in the pre-send checks of /emails, /sms and
/calls. Human-origin orgs pass through untouched with one indexed lookup.
Counts live in agent_send_log — written here at check time, so the caps
count *attempts*, uniformly across channels, with no dependency on the
per-channel message tables.

Every recipient of a send counts, not just one representative address —
email's to/cc/bcc can fan out to many recipients per call, and a cap that
only looked at the first one would let that fan-out defeat the caps
entirely. Callers pass the full (deduped, normalized) recipient set for
the send; one AgentSendLog row is written per recipient on allow.

Spec: hail-website/docs/superpowers/specs/2026-07-14-agent-self-signup-design.md
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import AgentSendLog, Organization, PlatformFlag

__all__ = [
    "AGENT_OUTBOUND_DISABLED_FLAG",
    "AgentCapDenial",
    "agent_outbound_halted",
    "check_agent_send_allowed",
]

AGENT_OUTBOUND_DISABLED_FLAG = "agent_outbound_disabled"

_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class AgentCapDenial:
    reason: str
    retry_after_seconds: int


def _caps(channel: str) -> tuple[int, int, int, int]:
    """(per_hour, per_day, recipients_per_day, global_per_hour) for channel."""
    return {
        "email": (
            settings.agent_email_per_hour,
            settings.agent_email_per_day,
            settings.agent_email_recipients_per_day,
            settings.agent_global_email_per_hour,
        ),
        "sms": (
            settings.agent_sms_per_hour,
            settings.agent_sms_per_day,
            settings.agent_sms_recipients_per_day,
            settings.agent_global_sms_per_hour,
        ),
        "voice": (
            settings.agent_voice_per_hour,
            settings.agent_voice_per_day,
            settings.agent_voice_recipients_per_day,
            settings.agent_global_voice_per_hour,
        ),
    }[channel]


async def _is_agent_org(db: AsyncSession, organization_id: UUID) -> bool:
    stmt = select(Organization.origin).where(Organization.id == organization_id)
    return (await db.execute(stmt)).scalar_one_or_none() == "agent"


async def _kill_switch_on(db: AsyncSession) -> bool:
    stmt = select(PlatformFlag.value).where(
        PlatformFlag.key == AGENT_OUTBOUND_DISABLED_FLAG
    )
    return (await db.execute(stmt)).scalar_one_or_none() == "true"


async def agent_outbound_halted(db: AsyncSession, organization_id: UUID) -> bool:
    """True iff this org is agent-origin AND the platform kill switch is on.

    The inbound-forward relay worker uses this instead of the full velocity
    gate: a forward is not an agent-initiated send, so per-workspace velocity
    caps do not apply to it, but the emergency kill switch must still halt it —
    the runbook promises the switch disables ALL agent outbound, forwards on
    shared sender domains included. Human orgs return False with one lookup.
    """
    if not await _is_agent_org(db, organization_id):
        return False
    return await _kill_switch_on(db)


async def check_agent_send_allowed(
    db: AsyncSession, organization_id: UUID, channel: str, recipients: list[str]
) -> AgentCapDenial | None:
    """None => allowed (and, for agent orgs, the attempt was logged — one
    AgentSendLog row per entry in ``recipients``).

    ``recipients`` is the full recipient set for this send (e.g. to+cc+bcc
    for email, deduped and normalized the same way the compliance gate
    screens them; a single-element list for sms/calls). Every recipient
    counts toward the hourly/daily attempt caps and the distinct-recipient
    cap — not just one representative address, which would let a large
    cc/bcc fan-out defeat the caps entirely.

    Denials carry a caller-safe reason and a Retry-After hint. Ordering:
    origin check (cheapest, exits for all human traffic) -> kill switch ->
    per-org hourly/daily -> distinct recipients -> global ceiling.
    """
    if not await _is_agent_org(db, organization_id):
        return None

    if await _kill_switch_on(db):
        return AgentCapDenial(
            reason="agent outbound is temporarily disabled platform-wide; retry later",
            retry_after_seconds=3600,
        )

    per_hour, per_day, recipients_per_day, global_per_hour = _caps(channel)
    now = datetime.now(timezone.utc)
    n = len(recipients)

    # One round trip for both org windows: hour is a subset of the day window,
    # so count(*) FILTER(...) subdivides a single day-scoped scan (same idiom
    # as compliance_gate._check_velocity).
    org_counts = (
        await db.execute(
            select(
                func.count()
                .filter(AgentSendLog.created_at > now - _HOUR)
                .label("hour"),
                func.count().filter(AgentSendLog.created_at > now - _DAY).label("day"),
            ).where(
                AgentSendLog.organization_id == organization_id,
                AgentSendLog.channel == channel,
                AgentSendLog.created_at > now - _DAY,
            )
        )
    ).one()
    org_hour, org_day = org_counts.hour, org_counts.day
    if org_hour + n > per_hour:
        return AgentCapDenial(
            reason=f"{channel} cap reached: {per_hour}/hour per workspace",
            retry_after_seconds=3600,
        )

    if org_day + n > per_day:
        return AgentCapDenial(
            reason=f"{channel} cap reached: {per_day}/day per workspace",
            retry_after_seconds=6 * 3600,
        )

    already_contacted = {
        row[0]
        for row in (
            await db.execute(
                select(func.distinct(AgentSendLog.recipient)).where(
                    AgentSendLog.organization_id == organization_id,
                    AgentSendLog.channel == channel,
                    AgentSendLog.created_at > now - _DAY,
                )
            )
        ).all()
    }
    # Union, not sum: a recipient already contacted in the window is still
    # allowed even once the cap is reached (only *new* recipients push the
    # union past the limit).
    if len(already_contacted | set(recipients)) > recipients_per_day:
        return AgentCapDenial(
            reason=f"{channel} distinct-recipient cap reached: {recipients_per_day}/day",
            retry_after_seconds=6 * 3600,
        )

    global_hour = (
        await db.execute(
            select(func.count()).where(
                AgentSendLog.channel == channel, AgentSendLog.created_at > now - _HOUR
            )
        )
    ).scalar_one()
    if global_hour + n > global_per_hour:
        return AgentCapDenial(
            reason=f"platform-wide agent {channel} ceiling reached; retry later",
            retry_after_seconds=1800,
        )

    for r in recipients:
        db.add(
            AgentSendLog(organization_id=organization_id, channel=channel, recipient=r)
        )
    await db.flush()
    return None
