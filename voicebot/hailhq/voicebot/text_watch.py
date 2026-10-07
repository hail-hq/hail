"""Feed a text the caller sends during a call into the live voice session.

The API marks such a text's auto-reply ``skipped`` (see
``hailhq.core.sms_ingest``). This watcher polls ``sms`` for new inbound texts
of the call's thread and hands each to the voice agent as a user message.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from hailhq.core import threads
from hailhq.core.db import session_scope
from hailhq.core.models import Sms
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("hailhq.voicebot")

POLL_SECONDS = 2.0
MAX_INJECT_CHARS = 1000


async def new_inbound_texts(
    db: AsyncSession,
    organization_id: UUID,
    agent_id: UUID,
    caller_e164: str,
    after: datetime,
) -> list[Sms]:
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == organization_id,
            Sms.agent_id == agent_id,
            Sms.direction == "inbound",
            Sms.from_e164 == caller_e164,
            Sms.requested_at > after,
        )
        .order_by(Sms.requested_at)
    )
    return list((await db.execute(stmt)).scalars().all())


async def watch_incoming_texts(
    session: Any, call_id: UUID, *, poll_seconds: float = POLL_SECONDS
) -> None:
    """Run until cancelled. A failed poll is logged and retried."""
    try:
        async with session_scope() as db:
            key = await threads.call_thread_key(db, call_id)
    except Exception:
        logger.exception("call_id=%s text watch could not start", call_id)
        return
    if key is None:
        return
    org, agent_id, caller = key
    cursor = datetime.now(timezone.utc)
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            async with session_scope() as db:
                rows = await new_inbound_texts(db, org, agent_id, caller, cursor)
            for row in rows:
                cursor = max(cursor, row.requested_at)
                session.generate_reply(
                    user_input=threads.TEXT_MARKER + (row.body or "")[:MAX_INJECT_CHARS]
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("call_id=%s text watch poll failed", call_id)
