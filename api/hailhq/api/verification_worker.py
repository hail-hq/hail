"""Background pass over carrier verifications.

Every ``poll_interval`` seconds: release the numbers and cancel the carrier
registrations of closed organizations (``hailhq.api.org_close``), submit
drafts still waiting (the carrier was down or refused at creation), and pull
the carrier's answer for submitted ones so a customer does not have to open
the console for the state to move. Last, ask the carrier whether each US
trial texting number is still approved (``refresh_trial_approvals``).
Same run_forever / stop contract as ``AbuseMonitorWorker``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from hailhq.api.org_close import close_org_carrier_items
from hailhq.api.routes.calls import get_livekit_optional
from hailhq.api.routes.verifications import sweep_verifications
from hailhq.core.providers.verification import get_verification_provider
from hailhq.core.sms_trial import refresh_trial_approvals

logger = logging.getLogger(__name__)


class VerificationWorker:
    def __init__(self, *, session_factory, poll_interval: float = 600.0) -> None:
        self._session_factory = session_factory
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — logged + retried next tick
                logger.exception("verification worker tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> dict[str, int]:
        async with self._session_factory() as session:
            # First: the sweep below skips closed orgs and ends by clearing
            # their answers.
            try:
                await close_org_carrier_items(
                    session,
                    datetime.now(timezone.utc),
                    get_verification_provider,
                    await get_livekit_optional(),
                )
            except Exception:
                await session.rollback()
                logger.exception("verification worker: account close step failed")
            counts = await sweep_verifications(session, get_verification_provider)
            # Its own step: a carrier error here must not stop the sweep.
            try:
                trial = await refresh_trial_approvals(session)
                counts["trial_not_approved"] = trial["not_approved"]
                counts["trial_no_answer"] = trial["no_answer"]
            except Exception:
                await session.rollback()
                logger.exception("verification worker: trial number check failed")
            if any(counts.values()):
                logger.info("verification worker: %s", counts)
            return counts
