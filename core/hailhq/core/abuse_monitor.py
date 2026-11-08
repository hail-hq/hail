"""Abuse-monitoring guardrail for SMS and email.

SMS: each organization registers its own US A2P 10DLC brand and campaign (see
docs/superpowers/specs/2026-10-13-us-sms-registration-design.md). One
org's abusive traffic still risks Hail's standing with the carrier, so this
module computes each org's rolling opt-out rate over a
configurable window and inserts a ChannelSuspension row when it crosses a
threshold — the mitigation for that risk, not optional
polish.

Email: every org sends through one shared SES account, so one org's hard
bounces or spam complaints can get sending paused for every org. The email
pass computes each org's hard bounce rate and complaint rate over a window
and suspends the org's email channel when either crosses its threshold.
Only the org's own sends count (POST /emails and agent sends): forwards and
Hail's system mail are left out, matching the suppression rule in
``email_delivery_events``, and the forward worker does not check
suspensions, so a suspended org's forwards keep going out.

Thresholds (core/hailhq/core/config.py's hail_sms_abuse_* and
hail_email_abuse_* settings) are explicitly a starting guess pending real
traffic data, per the design spec's own caution — expect to tune
post-launch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from hailhq.core.config import settings
from hailhq.core.models import (
    ChannelSuspension,
    Email,
    EmailEvent,
    Sms,
    Suppression,
    UsageEvent,
)
from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = [
    "AbuseMonitorWorker",
    "check_and_suspend_abusive_orgs",
    "check_and_suspend_email_orgs",
]


async def check_and_suspend_abusive_orgs(db: AsyncSession) -> int:
    """Scan for orgs whose SMS opt-out rate over the configured window
    exceeds the threshold, and suspend any not already suspended. Returns
    the count of orgs newly suspended this run."""
    window_start = datetime.now(timezone.utc) - timedelta(
        hours=settings.hail_sms_abuse_window_hours
    )
    # Read before the counts; see _suspended_orgs.
    already_suspended = await _suspended_orgs(db, "sms")
    last_lift = _last_lift("sms")

    send_stmt = (
        select(UsageEvent.organization_id, func.count())
        .outerjoin(last_lift, last_lift.c.organization_id == UsageEvent.organization_id)
        .where(
            UsageEvent.channel == "sms",
            # The plain bound lets the (channel, occurred_at) index limit
            # the scan; GREATEST then narrows it after a lift.
            UsageEvent.occurred_at >= window_start,
            UsageEvent.occurred_at >= _counted_from(window_start, last_lift),
        )
        .group_by(UsageEvent.organization_id)
    )
    send_counts = dict((await db.execute(send_stmt)).all())

    # Count DISTINCT opted-out recipients, not raw suppression rows: a single
    # recipient can text STOP repeatedly (each a new row, no unique
    # constraint), and rows-not-recipients would let one persistent recipient
    # push an org over the threshold and suspend its whole channel.
    opt_out_stmt = (
        select(
            Suppression.organization_id,
            func.count(func.distinct(Suppression.recipient)),
        )
        .outerjoin(
            last_lift, last_lift.c.organization_id == Suppression.organization_id
        )
        .where(
            Suppression.channel == "sms",
            Suppression.source == "stop_keyword",
            Suppression.created_at >= window_start,
            Suppression.created_at >= _counted_from(window_start, last_lift),
            Suppression.organization_id.is_not(None),
            # After a lift, a STOP counts only if the org texted that number
            # after the lift: late replies to the old blast must not suspend
            # it again against its few new sends. sent_at, not created_at:
            # a text queued before the lift and sent after it counts.
            or_(
                last_lift.c.lifted_at.is_(None),
                select(Sms.id)
                .where(
                    Sms.organization_id == Suppression.organization_id,
                    Sms.direction == "outbound",
                    Sms.to_e164 == Suppression.recipient,
                    Sms.sent_at >= last_lift.c.lifted_at,
                )
                .exists(),
            ),
        )
        .group_by(Suppression.organization_id)
    )
    opt_out_counts = dict((await db.execute(opt_out_stmt)).all())

    window = settings.hail_sms_abuse_window_hours
    max_rate = settings.hail_sms_abuse_max_opt_out_rate
    suspended = 0
    for org_id, sends in send_counts.items():
        if org_id in already_suspended:
            continue
        if sends < settings.hail_sms_abuse_min_sends:
            continue
        opt_outs = opt_out_counts.get(org_id, 0)
        rate = opt_outs / sends
        if rate <= max_rate:
            continue
        reason = (
            f"opt-out rate {rate:.1%} over {window}h "
            f"window ({opt_outs}/{sends}) exceeds {max_rate:.1%} threshold"
        )
        if await _suspend(db, org_id, "sms", reason):
            suspended += 1
    return suspended


async def _suspended_orgs(db: AsyncSession, channel: str) -> set:
    """Orgs with an active suspension on ``channel``. Callers read this
    before their counts: a lift that commits during the pass then leaves the
    org in this set, so the pass skips it instead of suspending it again on
    counts from before the lift."""
    return set(
        (
            await db.execute(
                select(ChannelSuspension.organization_id).where(
                    ChannelSuspension.channel == channel,
                    ChannelSuspension.lifted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


def _last_lift(channel: str):
    """Subquery: each org's latest lift time on ``channel``. After an
    operator lifts a suspension, only activity after the lift counts, so
    the activity that caused it does not suspend the org again."""
    return (
        select(
            ChannelSuspension.organization_id,
            func.max(ChannelSuspension.lifted_at).label("lifted_at"),
        )
        .where(
            ChannelSuspension.channel == channel,
            ChannelSuspension.lifted_at.is_not(None),
        )
        .group_by(ChannelSuspension.organization_id)
        .subquery()
    )


def _counted_from(window_start: datetime, last_lift):
    # GREATEST ignores NULL: an org never lifted counts the whole window.
    return func.greatest(window_start, last_lift.c.lifted_at)


async def _suspend(db: AsyncSession, org_id, channel: str, reason: str) -> bool:
    """Insert an active suspension. Two API replicas can tick at once: the
    partial unique index on active (org, channel) rows keeps one and the
    other insert does nothing. Returns True iff a row was inserted."""
    inserted = (
        await db.execute(
            pg_insert(ChannelSuspension)
            .values(organization_id=org_id, channel=channel, reason=reason)
            .on_conflict_do_nothing()
            .returning(ChannelSuspension.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return False
    logger.warning(
        "suspending org=%s %s channel for abuse: %s", org_id, channel, reason
    )
    return True


def _bare_address(column):
    """SQL: the address inside "Name <address>" if present, trimmed and
    lowercased."""
    return func.lower(func.trim(func.regexp_replace(column, r"^.*<([^>]*)>.*$", r"\1")))


def _org_own_send():
    """Filter for the org's own outbound sends: not a forward, not system
    mail."""
    return and_(
        Email.direction == "outbound",
        ~Email.metadata_.has_key("forwarded_from"),
        ~Email.metadata_.has_key("system_kind"),
    )


async def check_and_suspend_email_orgs(db: AsyncSession) -> int:
    """Scan for orgs whose email hard bounce rate or complaint rate over the
    configured window exceeds its threshold, and suspend any not already
    suspended. Rates are per recipient, as SES counts them. Returns the
    count of orgs newly suspended this run."""
    window_start = datetime.now(timezone.utc) - timedelta(
        hours=settings.hail_email_abuse_window_hours
    )
    # Read before the counts; see _suspended_orgs.
    already_suspended = await _suspended_orgs(db, "email")
    last_lift = _last_lift("email")
    in_window = and_(
        _org_own_send(),
        # The plain bound lets emails_outbound_sent_at_idx limit the scan.
        Email.sent_at >= window_start,
        Email.sent_at >= _counted_from(window_start, last_lift),
    )

    # Each distinct address counts once per email, so repeating one address
    # in to/cc/bcc (also as "Name <address>") cannot inflate the denominator
    # and hide a high rate. ``||`` with a NULL cc/bcc array returns the other
    # side unchanged.
    addr = (
        func.unnest(
            Email.to_addresses.op("||")(Email.cc_addresses).op("||")(
                Email.bcc_addresses
            )
        )
        .table_valued("addr")
        .render_derived(name="recipients")
        .lateral()
    )
    email_recipients = (
        select(
            Email.id.label("email_id"),
            Email.organization_id.label("organization_id"),
            func.count(func.distinct(_bare_address(addr.c.addr))).label("recipients"),
        )
        .select_from(Email)
        .join(addr, true())
        .outerjoin(last_lift, last_lift.c.organization_id == Email.organization_id)
        .where(in_window)
        .group_by(Email.id)
        # A CTE used twice below: Postgres computes it once.
        .cte("email_recipients")
    )

    # One SES event can name several recipients; each (email, recipient)
    # counts once per kind, so a repeated feedback report for the same
    # recipient does not count twice. The parser always stores a list.
    event_addr = (
        func.jsonb_array_elements_text(EmailEvent.payload["recipients"])
        .table_valued("addr")
        .render_derived(name="event_recipients")
        .lateral()
    )
    hard_bounce = and_(
        EmailEvent.kind == "bounced",
        EmailEvent.payload["hard"].as_boolean().is_(True),
        # SES did not try these: the address is on the shared SES account
        # list because of another org. Not this org's bounce.
        func.coalesce(EmailEvent.payload["bounce_sub_type"].as_string(), "")
        != "OnAccountSuppressionList",
    )
    complained = EmailEvent.kind == "complained"
    named = _bare_address(event_addr.c.addr)

    def _per_email(kind_filter):
        # Named recipients count once each; an event with no recipients
        # (redacted by the mailbox provider) counts once per event. The sum
        # is capped at the email's distinct recipients (the same count as
        # the denominator), so repeated reports cannot count more people
        # than the email went to.
        return func.least(
            func.count(func.distinct(named)).filter(kind_filter, named.is_not(None))
            + func.count(func.distinct(EmailEvent.id)).filter(
                kind_filter, event_addr.c.addr.is_(None)
            ),
            email_recipients.c.recipients,
        )

    sends = (
        select(
            email_recipients.c.organization_id,
            func.sum(email_recipients.c.recipients).label("sends"),
        )
        .group_by(email_recipients.c.organization_id)
        # Orgs under the floor are never flagged; skip their event work.
        .having(
            func.sum(email_recipients.c.recipients)
            >= settings.hail_email_abuse_min_sends
        )
        .subquery()
    )
    # Joined to the denominator's emails: a late complaint on mail sent
    # before the window must not count against a smaller recent send.
    per_email = (
        select(
            email_recipients.c.organization_id,
            _per_email(hard_bounce).label("bounces"),
            _per_email(complained).label("complaints"),
        )
        .select_from(EmailEvent)
        .join(email_recipients, email_recipients.c.email_id == EmailEvent.email_id)
        .outerjoin(event_addr, true())
        .where(
            or_(hard_bounce, complained),
            # Events come after the send, so this bound is safe and lets
            # the (email_id, occurred_at) index limit the scan.
            EmailEvent.occurred_at >= window_start,
            email_recipients.c.organization_id.in_(select(sends.c.organization_id)),
        )
        .group_by(
            email_recipients.c.email_id,
            email_recipients.c.organization_id,
            email_recipients.c.recipients,
        )
        .subquery()
    )
    events = (
        select(
            per_email.c.organization_id,
            func.sum(per_email.c.bounces).label("bounces"),
            func.sum(per_email.c.complaints).label("complaints"),
        )
        .group_by(per_email.c.organization_id)
        .subquery()
    )
    rates_stmt = select(
        sends.c.organization_id,
        sends.c.sends,
        func.coalesce(events.c.bounces, 0),
        func.coalesce(events.c.complaints, 0),
    ).outerjoin(events, events.c.organization_id == sends.c.organization_id)
    rows = (await db.execute(rates_stmt)).all()

    window = settings.hail_email_abuse_window_hours
    max_bounce = settings.hail_email_abuse_max_bounce_rate
    max_complaint = settings.hail_email_abuse_max_complaint_rate

    suspended = 0
    for org_id, sent_to, bounces, complaints in rows:
        sends, bounces, complaints = int(sent_to), int(bounces), int(complaints)
        if org_id in already_suspended:
            continue
        bounce_rate = bounces / sends
        complaint_rate = complaints / sends
        reasons = []
        if bounce_rate > max_bounce:
            reasons.append(
                f"hard bounce rate {bounce_rate:.1%} over {window}h window "
                f"({bounces}/{sends}) exceeds {max_bounce:.1%} threshold"
            )
        if complaint_rate > max_complaint:
            reasons.append(
                f"complaint rate {complaint_rate:.2%} over {window}h window "
                f"({complaints}/{sends}) exceeds {max_complaint:.2%} threshold"
            )
        if not reasons:
            continue
        if await _suspend(db, org_id, "email", "; ".join(reasons)):
            suspended += 1
    return suspended


class AbuseMonitorWorker:
    """Periodic poller that runs :func:`check_and_suspend_abusive_orgs` (SMS)
    and :func:`check_and_suspend_email_orgs` on a fixed interval, mirroring
    ``DomainVerificationWorker``'s session_factory / poll_interval /
    run_forever / stop contract so it plugs into ``main.py``'s lifespan and
    ``_stop_worker`` helper unchanged.
    """

    def __init__(self, *, session_factory, poll_interval: float = 3600.0) -> None:
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("abuse monitor worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        # One session and commit per channel, and an error in one channel's
        # pass is logged without skipping or rolling back the other.
        count = 0
        for check in (check_and_suspend_abusive_orgs, check_and_suspend_email_orgs):
            try:
                async with self._session_factory() as session:
                    suspended = await check(session)
                    await session.commit()
                count += suspended
            except Exception:
                logger.exception("abuse monitor %s failed", check.__name__)
        if count:
            logger.info("abuse monitor suspended %d org(s) this run", count)
        return count
