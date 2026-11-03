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
    Suppression,
    UsageEvent,
)
from sqlalchemy import Text, and_, case, cast, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import JSONB
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

    send_stmt = (
        select(UsageEvent.organization_id, func.count())
        .where(UsageEvent.channel == "sms", UsageEvent.occurred_at >= window_start)
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
        .where(
            Suppression.channel == "sms",
            Suppression.source == "stop_keyword",
            Suppression.created_at >= window_start,
            Suppression.organization_id.is_not(None),
        )
        .group_by(Suppression.organization_id)
    )
    opt_out_counts = dict((await db.execute(opt_out_stmt)).all())

    already_suspended = await _suspended_orgs(db, "sms")

    suspended = 0
    for org_id, sends in send_counts.items():
        if org_id in already_suspended:
            continue
        if sends < settings.hail_sms_abuse_min_sends:
            continue
        opt_outs = opt_out_counts.get(org_id, 0)
        rate = opt_outs / sends
        if rate > settings.hail_sms_abuse_max_opt_out_rate:
            db.add(
                ChannelSuspension(
                    organization_id=org_id,
                    channel="sms",
                    reason=(
                        f"opt-out rate {rate:.1%} over {settings.hail_sms_abuse_window_hours}h "
                        f"window ({opt_outs}/{sends}) exceeds "
                        f"{settings.hail_sms_abuse_max_opt_out_rate:.1%} threshold"
                    ),
                )
            )
            logger.warning(
                "suspending org=%s sms channel for abuse: rate=%.1f%%",
                org_id,
                rate * 100,
            )
            suspended += 1
    await db.flush()
    return suspended


async def _suspended_orgs(db: AsyncSession, channel: str) -> set:
    return set(
        (
            await db.execute(
                select(ChannelSuspension.organization_id).where(
                    ChannelSuspension.channel == channel
                )
            )
        )
        .scalars()
        .all()
    )


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
    send_stmt = (
        select(
            Email.organization_id,
            func.count(
                func.distinct(func.concat(Email.id, ":", _bare_address(addr.c.addr)))
            ),
        )
        .select_from(Email)
        .join(addr, true())
        .where(_org_own_send(), Email.sent_at >= window_start)
        .group_by(Email.organization_id)
    )
    send_counts = dict((await db.execute(send_stmt)).all())

    # One SES event can name several recipients; each (email, recipient)
    # counts once per kind, so a repeated feedback report for the same
    # recipient does not count twice. A complaint whose recipients the
    # mailbox provider redacted still counts as one (keyed by the event).
    payload_recipients = EmailEvent.payload["recipients"]
    event_addr = (
        func.jsonb_array_elements_text(
            case(
                (
                    func.jsonb_typeof(payload_recipients) == "array",
                    payload_recipients,
                ),
                else_=cast(literal("[]"), JSONB),
            )
        )
        .table_valued("addr")
        .render_derived(name="event_recipients")
        .lateral()
    )
    event_key = func.concat(
        EmailEvent.email_id,
        ":",
        func.coalesce(_bare_address(event_addr.c.addr), cast(EmailEvent.id, Text)),
    )
    hard_bounce = and_(
        EmailEvent.kind == "bounced",
        EmailEvent.payload["hard"].as_boolean().is_(True),
        # SES did not try these: the address is on the shared SES account
        # list because of another org. Not this org's bounce.
        func.coalesce(EmailEvent.payload["bounce_sub_type"].as_string(), "")
        != "OnAccountSuppressionList",
    )
    event_stmt = (
        select(
            EmailEvent.organization_id,
            func.count(func.distinct(event_key)).filter(hard_bounce),
            func.count(func.distinct(event_key)).filter(
                EmailEvent.kind == "complained"
            ),
        )
        .select_from(EmailEvent)
        .join(Email, Email.id == EmailEvent.email_id)
        .outerjoin(event_addr, true())
        .where(
            _org_own_send(),
            # Same emails as the denominator: a late complaint on mail sent
            # before the window must not count against a smaller recent send.
            Email.sent_at >= window_start,
            EmailEvent.occurred_at >= window_start,
            or_(hard_bounce, EmailEvent.kind == "complained"),
        )
        .group_by(EmailEvent.organization_id)
    )
    event_counts = {
        org_id: (bounces, complaints)
        for org_id, bounces, complaints in (await db.execute(event_stmt)).all()
    }

    already_suspended = await _suspended_orgs(db, "email")
    window = settings.hail_email_abuse_window_hours
    max_bounce = settings.hail_email_abuse_max_bounce_rate
    max_complaint = settings.hail_email_abuse_max_complaint_rate

    suspended = 0
    for org_id, sends in send_counts.items():
        if org_id in already_suspended:
            continue
        if sends < settings.hail_email_abuse_min_sends:
            continue
        bounces, complaints = event_counts.get(org_id, (0, 0))
        bounce_rate = bounces / sends
        complaint_rate = complaints / sends
        if bounce_rate > max_bounce:
            reason = (
                f"hard bounce rate {bounce_rate:.1%} over {window}h window "
                f"({bounces}/{sends}) exceeds {max_bounce:.1%} threshold"
            )
        elif complaint_rate > max_complaint:
            reason = (
                f"complaint rate {complaint_rate:.2%} over {window}h window "
                f"({complaints}/{sends}) exceeds {max_complaint:.2%} threshold"
            )
        else:
            continue
        # Two API replicas can tick at once: the unique (org, channel) key
        # keeps one row and the other insert does nothing.
        inserted = (
            await db.execute(
                pg_insert(ChannelSuspension)
                .values(organization_id=org_id, channel="email", reason=reason)
                .on_conflict_do_nothing()
                .returning(ChannelSuspension.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue
        logger.warning("suspending org=%s email channel for abuse: %s", org_id, reason)
        suspended += 1
    await db.flush()
    return suspended


class AbuseMonitorWorker:
    """Periodic poller that runs :func:`check_and_suspend_abusive_orgs` (SMS)
    and :func:`check_and_suspend_email_orgs` on a fixed interval, mirroring ``DomainVerificationWorker``'s
    session_factory / poll_interval / run_forever / stop contract so it
    plugs into ``main.py``'s lifespan and ``_stop_worker`` helper unchanged.
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
        # One session and commit per channel: an email pass error must not
        # roll back the SMS suspensions from the same tick.
        count = 0
        for check in (check_and_suspend_abusive_orgs, check_and_suspend_email_orgs):
            async with self._session_factory() as session:
                count += await check(session)
                await session.commit()
        if count:
            logger.info("abuse monitor suspended %d org(s) this run", count)
        return count
