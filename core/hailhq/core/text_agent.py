"""Text replies by an agent.

``ingest_inbound_sms`` marks an inbound row ``agent_reply_state = 'pending'``
when the number routes texts to a live agent with ``sms_enabled``. The
voicebot's text worker (``hailhq.voicebot.textbot``) claims pending rows,
builds the chat from the agent's instructions plus the recent thread, asks
the LLM for one reply, and sends it through ``POST /internal/agent/reply-sms``.
This module holds the DB side so the worker stays LLM-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from hailhq.core.config import settings
from hailhq.core.models import Agent, PhoneNumber, Sms
from hailhq.core.prompts import TEXT_PREAMBLE, build_text_instructions
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CLAIM_LEASE",
    "MAX_ATTEMPTS",
    "MAX_REPLIES_PER_THREAD",
    "MAX_REPLY_CHARS",
    "RETRY_BACKOFF",
    "TEXT_PREAMBLE",
    "THREAD_LIMIT",
    "THREAD_WINDOW",
    "ClaimedReply",
    "ReplyState",
    "answers_texts",
    "build_chat_messages",
    "claim_pending_reply",
    "finish_reply",
    "replies_in_thread",
    "retry_reply",
    "should_queue_reply",
    "thread_messages",
]

MAX_REPLY_CHARS = 480  # same cap as the voice send_sms tool (about 3 segments)
THREAD_LIMIT = 20  # messages of history given to the model
THREAD_WINDOW = timedelta(hours=24)
# At most this many agent replies in one thread per THREAD_WINDOW (a rolling
# 24 hours, no reset when a person writes): past it the agent stays quiet until
# older replies leave the window. A loop breaker.
MAX_REPLIES_PER_THREAD = 20

# A worker that has not finished within this long is presumed dead.
CLAIM_LEASE = timedelta(minutes=2)
# Claims per text (the first try included) before it is marked failed.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(seconds=30)  # doubled after each failed attempt

ReplyState = Literal["done", "skipped", "failed"]


@dataclass(frozen=True)
class ClaimedReply:
    sms: Sms
    agent: Agent
    number: PhoneNumber
    attempt: int  # which claim this is, 1-based


def answers_texts(agent: Agent | None) -> bool:
    """True for a live agent with ``sms_enabled``: the one test for queueing,
    claiming and sending a text reply."""
    return agent is not None and agent.status == "live" and agent.sms_enabled


async def should_queue_reply(db: AsyncSession, number: PhoneNumber) -> bool:
    """True when this number's text agent exists, is live and answers texts."""
    if number.sms_agent_id is None:
        return False
    return answers_texts(await db.get(Agent, number.sms_agent_id))


def _claimable(now: datetime):
    """Rows no worker is handling: pending, or processing with a dead lease."""
    return or_(
        Sms.agent_reply_state == "pending",
        and_(
            Sms.agent_reply_state == "processing",
            Sms.agent_reply_available_at <= now,
        ),
    )


async def _expire_unclaimable(db: AsyncSession, now: datetime) -> None:
    """Close rows that must not be answered: too old, or out of attempts."""
    base = (Sms.direction == "inbound", _claimable(now))
    cutoff = now - timedelta(seconds=settings.hail_text_reply_max_age_seconds)
    await db.execute(
        update(Sms)
        .where(*base, Sms.requested_at < cutoff)
        .values(agent_reply_state="skipped")
    )
    await db.execute(
        update(Sms)
        .where(*base, Sms.agent_reply_attempts >= MAX_ATTEMPTS)
        .values(agent_reply_state="failed")
    )


async def claim_pending_reply(db: AsyncSession) -> ClaimedReply | None:
    """Claim the oldest answerable inbound text that still has an agent, or None.

    ``FOR UPDATE SKIP LOCKED`` lets several workers (and replicas) poll the
    same table. The claim sets ``processing`` with a lease and commits, so the
    row lock and the connection are free before the slow LLM and API calls. A
    crashed worker's lease runs out and the row is claimed again, up to
    ``MAX_ATTEMPTS`` claims. Texts older than
    ``HAIL_TEXT_REPLY_MAX_AGE_SECONDS`` are skipped, not answered late.
    """
    now = datetime.now(timezone.utc)
    await _expire_unclaimable(db, now)
    stmt = (
        select(Sms)
        .where(
            Sms.direction == "inbound",
            or_(
                and_(
                    Sms.agent_reply_state == "pending",
                    or_(
                        Sms.agent_reply_available_at.is_(None),
                        Sms.agent_reply_available_at <= now,
                    ),
                ),
                and_(
                    Sms.agent_reply_state == "processing",
                    Sms.agent_reply_available_at <= now,
                ),
            ),
        )
        .order_by(Sms.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    while True:
        sms = (await db.execute(stmt)).scalar_one_or_none()
        if sms is None:
            await db.commit()  # keep the expiry updates
            return None
        number = (
            await db.get(PhoneNumber, sms.to_number_id) if sms.to_number_id else None
        )
        agent = (
            await db.get(Agent, number.sms_agent_id)
            if number is not None and number.sms_agent_id
            else None
        )
        if number is not None and agent is not None and answers_texts(agent):
            sms.agent_reply_state = "processing"
            sms.agent_reply_attempts += 1
            sms.agent_reply_available_at = now + CLAIM_LEASE
            attempt = sms.agent_reply_attempts
            await db.commit()
            return ClaimedReply(sms=sms, agent=agent, number=number, attempt=attempt)
        # Routing changed between ingest and now: drop it quietly and take the
        # next row, so a backlog of dropped texts does not cost a poll each.
        await finish_reply(db, sms, "skipped")


def _thread_filter(a: str, b: str):
    return or_(
        and_(Sms.from_e164 == a, Sms.to_e164 == b),
        and_(Sms.from_e164 == b, Sms.to_e164 == a),
    )


def thread_lock_key(sms: Sms) -> str:
    """Advisory-lock key for one thread: same for both directions."""
    a, b = sorted((sms.from_e164, sms.to_e164))
    return f"reply-sms:{sms.organization_id}:{a}:{b}"


async def thread_messages(
    db: AsyncSession, sms: Sms, *, limit: int = THREAD_LIMIT
) -> list[Sms]:
    """The last ``limit`` messages between the two numbers (oldest first),
    including ``sms`` itself, within ``THREAD_WINDOW``."""
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == sms.organization_id,
            _thread_filter(sms.from_e164, sms.to_e164),
            Sms.requested_at >= since,
            Sms.requested_at <= sms.requested_at,
        )
        .order_by(Sms.requested_at.desc(), Sms.created_at.desc())
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def replies_in_thread(db: AsyncSession, sms: Sms) -> int:
    """Agent replies already sent in this thread within ``THREAD_WINDOW``."""
    since = datetime.now(timezone.utc) - THREAD_WINDOW
    stmt = select(func.count(Sms.id)).where(
        Sms.organization_id == sms.organization_id,
        Sms.direction == "outbound",
        Sms.agent_id.is_not(None),
        _thread_filter(sms.from_e164, sms.to_e164),
        Sms.requested_at >= since,
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


def build_chat_messages(agent: Agent, history: list[Sms]) -> list[dict[str, Any]]:
    """OpenAI-style messages: system (preamble + instructions) then the
    thread, inbound as ``user`` and outbound as ``assistant``."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_text_instructions(agent.system_prompt),
        }
    ]
    for row in history:
        role = "user" if row.direction == "inbound" else "assistant"
        messages.append({"role": role, "content": row.body})
    return messages


async def finish_reply(
    db: AsyncSession, sms: Sms, state: ReplyState, *, attempt: int | None = None
) -> None:
    """Record the final state. With ``attempt`` it only applies while that
    claim still owns the row (a reclaimed row is not overwritten by a worker
    that was presumed dead)."""
    stmt = update(Sms).where(Sms.id == sms.id)
    if attempt is not None:
        stmt = stmt.where(
            Sms.agent_reply_state == "processing",
            Sms.agent_reply_attempts == attempt,
        )
    await db.execute(stmt.values(agent_reply_state=state))
    await db.commit()


async def retry_reply(db: AsyncSession, claimed: ClaimedReply) -> str | None:
    """A transient error: put the text back to ``pending`` after a backoff, or
    mark it ``failed`` once ``MAX_ATTEMPTS`` claims are used. Returns the state
    written, or None when the claim no longer owns the row."""
    if claimed.attempt >= MAX_ATTEMPTS:
        await finish_reply(db, claimed.sms, "failed", attempt=claimed.attempt)
        return "failed"
    retry_at = datetime.now(timezone.utc) + RETRY_BACKOFF * 2 ** (claimed.attempt - 1)
    result = await db.execute(
        update(Sms)
        .where(
            Sms.id == claimed.sms.id,
            Sms.agent_reply_state == "processing",
            Sms.agent_reply_attempts == claimed.attempt,
        )
        .values(agent_reply_state="pending", agent_reply_available_at=retry_at)
    )
    await db.commit()
    return "pending" if result.rowcount else None  # type: ignore[attr-defined]
