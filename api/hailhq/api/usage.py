"""Shared usage-metering helper.

Appends one ``usage_events`` row in a fresh session and pings the website
rater. Best-effort — failures are logged, never re-raised — so a metering
hiccup can't roll back the user-facing operation. Used by both the outbound
send path and the inbound ingest path — both write ``channel='email'``
(direction is recorded on the ``emails`` row, not the usage channel).
"""

from __future__ import annotations

import logging
from uuid import UUID

from hailhq.core.db import session_scope
from hailhq.core.internal_webhook import notify_usage_event_recorded
from hailhq.core.models import UsageEvent

logger = logging.getLogger(__name__)

__all__ = ["write_usage_event"]


async def write_usage_event(
    *,
    organization_id: UUID,
    channel: str,
    units: int,
    ref: str,
) -> None:
    try:
        async with session_scope() as session:
            usage = UsageEvent(
                organization_id=organization_id,
                channel=channel,
                units=units,
                ref=ref,
            )
            session.add(usage)
            await session.flush()
            usage_event_id = str(usage.id)
            await session.commit()
    except Exception:  # pragma: no cover - logged, never re-raised
        logger.warning("usage_events write failed for ref=%s", ref, exc_info=True)
        return
    notify_usage_event_recorded(usage_event_id)
