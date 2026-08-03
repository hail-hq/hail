"""SMS abuse-monitoring guardrail.

Because all orgs share one Hail-owned A2P 10DLC Brand/Campaign (see the
SMS design spec's Decision 2 and its accepted-risk note), one org's
abusive traffic risks getting the whole platform's SMS sending throttled
by carriers. This module computes each org's rolling opt-out rate over a
configurable window and inserts a ChannelSuspension row when it crosses a
threshold — the actual mitigation for that accepted risk, not optional
polish.

Thresholds (core/hailhq/core/config.py's hail_sms_abuse_* settings) are
explicitly a starting guess pending real traffic data, per the design
spec's own caution — expect to tune post-launch.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from hailhq.core.config import settings
from hailhq.core.models import ChannelSuspension, Suppression, UsageEvent
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["AbuseMonitorWorker", "check_and_suspend_abusive_orgs"]


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

    already_suspended = set(
        (
            await db.execute(
                select(ChannelSuspension.organization_id).where(
                    ChannelSuspension.channel == "sms"
                )
            )
        )
        .scalars()
        .all()
    )

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


class AbuseMonitorWorker:
    """Periodic poller that runs :func:`check_and_suspend_abusive_orgs` on a
    fixed interval, mirroring ``DomainVerificationWorker``'s
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
        async with self._session_factory() as session:
            count = await check_and_suspend_abusive_orgs(session)
            await session.commit()
            if count:
                logger.info("abuse monitor suspended %d org(s) this run", count)
            return count
