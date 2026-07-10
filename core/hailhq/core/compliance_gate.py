"""Pre-send compliance gate — suppression list, DNC scrub, premium-rate
blocks, and velocity caps for outbound calls, SMS, and emails.

Three call sites (``api/hailhq/api/routes/calls.py``, ``.../emails.py``,
and ``.../sms.py``'s ``create_call`` / ``create_email`` / ``create_sms``)
— that's the "two concrete uses" the repo's "no abstractions without two
concrete uses" tenet asks for, so the five checks below live in one
module instead of being scattered inline in each route. The two phone
channels (voice, SMS) share ``_check_phone_destination`` so the
destination scrubs — and their audit ``checks`` keys — cannot drift
between them.

Call ``check_call_allowed`` / ``check_email_allowed`` right after the
existing consent check (``hailhq.api.consent.enforce_consent``) and
before any provider dial/send. On ``GateResult.allowed is False`` the
route must 403 with ``reason`` and record an audit_log denial (this
module intentionally has no dependency on the API's audit helper, to
avoid a core→api import — the route owns that write). On
``allowed is True`` the route should still fold ``GateResult.checks``
into its own "call.create"/"email.create" audit payload, so a scrub
result is logged for every send, not just blocked ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import ChannelSuspension, Sms, Suppression, UsageEvent

__all__ = [
    "GateResult",
    "check_call_allowed",
    "check_email_allowed",
    "check_sms_allowed",
    "check_national_dnc",
    "add_suppression",
    "remove_suppression",
    "check_channel_suspended",
    "normalize_recipient",
]


@dataclass
class GateResult:
    allowed: bool
    reason: str | None = None
    # Structured detail — which checks ran and what they found. Merged into
    # the caller's audit-log payload regardless of ``allowed``, so a scrub
    # result is on record for every send attempt, not only denials.
    checks: dict[str, Any] = field(default_factory=dict)


def normalize_recipient(recipient: str) -> str:
    """Lowercase email addresses; leave E.164 numbers untouched (already
    canonical — digits and a leading '+' have no case).

    Exported (not module-private) so ``hailhq.core.dsar`` can match the
    same ``suppressions.recipient`` normalization instead of maintaining
    its own copy.
    """
    return recipient.strip().lower() if "@" in recipient else recipient.strip()


async def _suppression_hit(
    db: AsyncSession,
    organization_id: UUID,
    recipients: list[str],
    channel: str,
) -> Suppression | None:
    """First matching suppression row for any of ``recipients``.

    Matches ``(recipient, channel)`` OR ``(recipient, 'all')``, scoped to
    this org OR a NULL (platform-wide) row.
    """
    if not recipients:
        return None
    stmt = (
        select(Suppression)
        .where(Suppression.recipient.in_(recipients))
        .where(Suppression.channel.in_([channel, "all"]))
        .where(
            or_(
                Suppression.organization_id == organization_id,
                Suppression.organization_id.is_(None),
            )
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def check_national_dnc(e164: str) -> bool:
    """Check the US National Do Not Call registry (donotcall.gov).

    STUB: there is no live vendor integration for the national DNC
    registry today — a real check requires a paid subscription, which is
    a Bucket-1/founder task, not something wireable with real credentials
    here. Always returns ``False`` (not on the registry). Gated behind
    ``settings.hail_national_dnc_enabled`` (default ``False``) by the
    caller; flip that on and replace this body once a vendor contract +
    credentials land.
    """
    return False


async def _check_velocity(
    db: AsyncSession,
    model: type[Any],
    ts_col: Any,
    *,
    organization_id: UUID,
    extra_filters: list[Any] | None = None,
    per_hour: int,
    per_day: int,
    unit: str,
) -> tuple[dict[str, int], str | None]:
    """Shared hour/day velocity check for all channels — one query covers
    both windows (the day window is a superset of the hour window, so two
    separate ``COUNT`` round-trips were redundant). ``model``/``ts_col``/
    ``extra_filters`` name what to count: billed ``usage_events`` for voice
    and email, ``sms`` attempt rows for SMS (see ``check_sms_allowed``).

    Org scoping is applied here (``model.organization_id``), not left to
    the caller's filters — a caller that forgot it would silently count
    across all tenants and turn a per-org cap global.

    Returns the ``checks["velocity"]`` detail dict plus a deny reason, or
    ``None`` if under both caps."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    stmt = (
        select(
            func.count().filter(ts_col >= hour_ago).label("hour_count"),
            func.count().filter(ts_col >= day_ago).label("day_count"),
        )
        .select_from(model)
        .where(
            model.organization_id == organization_id,
            *(extra_filters or []),
            ts_col >= day_ago,
        )
    )
    row = (await db.execute(stmt)).one()
    checks = {"hour_count": row.hour_count, "day_count": row.day_count}

    if row.hour_count >= per_hour:
        return checks, (
            f"velocity cap exceeded: {row.hour_count} {unit} in the last hour "
            f"(limit {per_hour})"
        )
    if row.day_count >= per_day:
        return checks, (
            f"velocity cap exceeded: {row.day_count} {unit} in the last day "
            f"(limit {per_day})"
        )
    return checks, None


def _parse_blocked_prefixes() -> tuple[str, ...]:
    raw = settings.hail_blocked_e164_prefixes
    return tuple(p.strip() for p in raw.split(",") if p.strip())


async def _check_phone_destination(
    db: AsyncSession,
    organization_id: UUID,
    to_e164: str,
    channel: str,
    checks: dict[str, Any],
) -> str | None:
    """Destination scrubs shared by the phone channels (voice, SMS):
    suppression list, national DNC registry, premium-rate prefix block.

    The national-DNC scrub applies to both phone channels — US DNC/TSR
    rules cover marketing texts as well as calls, so enabling
    ``hail_national_dnc_enabled`` covers them at once.

    Mutates ``checks`` in place; returns a deny reason or ``None``.
    """
    hit = await _suppression_hit(db, organization_id, [to_e164], channel)
    checks["suppression_checked"] = True
    checks["suppression_hit"] = hit is not None

    national_hit = False
    checks["national_dnc_checked"] = settings.hail_national_dnc_enabled
    if settings.hail_national_dnc_enabled:
        national_hit = await check_national_dnc(to_e164)
    checks["national_dnc_hit"] = national_hit

    if hit is not None:
        return f"recipient is on the suppression list ({hit.reason})"
    if national_hit:
        return "recipient is on the national Do Not Call registry"

    for prefix in _parse_blocked_prefixes():
        if to_e164.startswith(prefix):
            checks["premium_rate_blocked"] = True
            return f"destination prefix {prefix!r} is blocked (premium-rate/high-risk)"
    checks["premium_rate_blocked"] = False
    return None


async def check_call_allowed(
    db: AsyncSession, organization_id: UUID, to_e164: str
) -> GateResult:
    """Pre-send checks for an outbound call: suppression, national DNC,
    premium-rate prefix block, then velocity cap.

    Note on the velocity cap: it counts ``usage_events`` rows
    (``channel='voice'``), which the voicebot writes at call *completion*
    (see ``voicebot/hailhq/voicebot/agent.py``), not at dial time. It is
    therefore a lagging signal against sustained abuse over the window,
    not an instantaneous burst cap — acceptable for a flat "new-account"
    cap, per the compliance-gate spec.
    """
    checks: dict[str, Any] = {}

    reason = await _check_phone_destination(
        db, organization_id, to_e164, "voice", checks
    )
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    velocity_checks, reason = await _check_velocity(
        db,
        UsageEvent,
        UsageEvent.occurred_at,
        organization_id=organization_id,
        extra_filters=[UsageEvent.channel == "voice"],
        per_hour=settings.hail_velocity_call_per_hour,
        per_day=settings.hail_velocity_call_per_day,
        unit="calls",
    )
    checks["velocity"] = velocity_checks
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    return GateResult(allowed=True, checks=checks)


async def check_sms_allowed(
    db: AsyncSession, organization_id: UUID, to_e164: str
) -> GateResult:
    """Pre-send checks for an outbound SMS: suppression, national DNC,
    premium-rate prefix block, then velocity cap. Mirrors
    ``check_call_allowed``'s single-E.164 shape (not
    ``check_email_allowed``'s list shape) — Twilio's Messages API is
    single-recipient per call.

    Unlike voice/email, the velocity cap counts ``sms`` rows (send
    *attempts*, by ``created_at``) rather than billed ``usage_events``
    — the route skips the usage write for carrier-rejected sends, so a
    usage-based count would never trip on exactly the traffic pattern
    (number-probing blasts that all fail) the cap exists to stop.
    Gate-blocked attempts insert no row and rightly don't count.
    """
    checks: dict[str, Any] = {}

    if await check_channel_suspended(db, organization_id, "sms"):
        checks["channel_suspended"] = True
        return GateResult(
            allowed=False,
            reason="SMS sending is suspended for this organization (contact support)",
            checks=checks,
        )
    checks["channel_suspended"] = False

    reason = await _check_phone_destination(db, organization_id, to_e164, "sms", checks)
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    velocity_checks, reason = await _check_velocity(
        db,
        Sms,
        # created_at, not requested_at: identical value for outbound rows
        # (both server_default now() in the same INSERT) and covered by
        # idx_sms_org_created — requested_at has no index.
        Sms.created_at,
        organization_id=organization_id,
        extra_filters=[Sms.direction == "outbound"],
        per_hour=settings.hail_velocity_sms_per_hour,
        per_day=settings.hail_velocity_sms_per_day,
        unit="texts",
    )
    checks["velocity"] = velocity_checks
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    return GateResult(allowed=True, checks=checks)


async def check_email_allowed(
    db: AsyncSession, organization_id: UUID, to_addresses: list[str]
) -> GateResult:
    """Pre-send checks for an outbound email: suppression, then velocity cap.

    ``to_addresses`` should include every recipient the caller wants
    screened (to/cc/bcc) — the gate blocks the whole send if any one of
    them is suppressed.
    """
    checks: dict[str, Any] = {}
    normalized = [normalize_recipient(a) for a in to_addresses]

    hit = await _suppression_hit(db, organization_id, normalized, "email")
    checks["suppression_checked"] = True
    checks["suppression_hit"] = hit is not None
    if hit is not None:
        return GateResult(
            allowed=False,
            reason=f"recipient {hit.recipient!r} is suppressed ({hit.reason})",
            checks=checks,
        )

    velocity_checks, reason = await _check_velocity(
        db,
        UsageEvent,
        UsageEvent.occurred_at,
        organization_id=organization_id,
        extra_filters=[UsageEvent.channel == "email"],
        per_hour=settings.hail_velocity_email_per_hour,
        per_day=settings.hail_velocity_email_per_day,
        unit="emails",
    )
    checks["velocity"] = velocity_checks
    if reason is not None:
        return GateResult(allowed=False, reason=reason, checks=checks)

    return GateResult(allowed=True, checks=checks)


async def add_suppression(
    db: AsyncSession,
    *,
    organization_id: UUID | None,
    recipient: str,
    channel: str,
    reason: str,
    source: str,
) -> Suppression:
    """Insert one suppression row. Flushes but does not commit — the
    caller owns the transaction (matches the rest of this codebase's
    session-handling convention, e.g. ``_resolve_sender`` in emails.py)."""
    row = Suppression(
        organization_id=organization_id,
        recipient=normalize_recipient(recipient),
        channel=channel,
        reason=reason,
        source=source,
    )
    db.add(row)
    await db.flush()
    return row


async def remove_suppression(
    db: AsyncSession, *, organization_id: UUID | None, recipient: str, channel: str
) -> bool:
    """Delete all suppression rows matching (recipient, channel), scoped to
    this org OR a platform-wide (NULL org) row — the mirror of
    ``add_suppression`` for the STOP->START re-subscribe flow.

    ``suppressions`` has no unique constraint on (recipient, channel,
    organization_id), so more than one row can match here — e.g. a
    duplicate org-scoped row, or an org-scoped row alongside a
    platform-wide (NULL org) one. A single-row delete would leave the
    recipient still suppressed by the leftover row and silently defeat
    the re-subscribe; this deletes every matching row instead. Flushes
    but does not commit, matching this module's session convention.
    Returns True iff at least one row was deleted."""
    normalized = normalize_recipient(recipient)
    stmt = delete(Suppression).where(
        Suppression.recipient == normalized,
        Suppression.channel == channel,
        or_(
            Suppression.organization_id == organization_id,
            Suppression.organization_id.is_(None),
        ),
    )
    result = await db.execute(stmt)
    await db.flush()
    return result.rowcount > 0


async def check_channel_suspended(
    db: AsyncSession, organization_id: UUID, channel: str
) -> bool:
    """True iff this org has an active ChannelSuspension for this channel."""
    stmt = select(ChannelSuspension).where(
        ChannelSuspension.organization_id == organization_id,
        ChannelSuspension.channel == channel,
    )
    return (await db.execute(stmt)).scalar_one_or_none() is not None
