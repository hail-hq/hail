"""Text replies by an agent.

``ingest_inbound_sms`` marks an inbound row ``agent_reply_state = 'pending'``
when the number routes texts to a live agent with ``sms_enabled``. The
voicebot's text worker (``hailhq.voicebot.textbot``) claims pending rows,
builds the chat from the agent's instructions plus the recent texts, asks
the LLM for one reply (it may read older texts and calls with the
``thread_history`` tool), and sends it through ``POST /internal/agent/reply-sms``.
This module holds the DB side so the worker stays LLM-only. The claim skips
texts of an org with no funds, so no model call runs for a reply the API
would refuse.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from hailhq.core.billing import has_funds
from hailhq.core.config import settings
from hailhq.core.models import Agent, PhoneNumber, Sms
from hailhq.core.prompts import TEXT_PREAMBLE, build_text_instructions
from hailhq.core.threads import ThreadItem, thread_items
from sqlalchemy import DateTime, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "CHAT_LIMIT",
    "CHAT_WINDOW",
    "CLAIM_LEASE",
    "INSUFFICIENT_FUNDS_SKIP",
    "MAX_ATTEMPTS",
    "MAX_REPLIES_PER_THREAD",
    "MAX_REPLY_CHARS",
    "RETRY_BACKOFF",
    "TEXT_PREAMBLE",
    "ClaimedReply",
    "ReplyState",
    "answers_texts",
    "build_chat_messages",
    "claim_pending_reply",
    "finish_reply",
    "prior_reply",
    "replies_in_thread",
    "retry_reply",
    "should_queue_reply",
    "thread_history_for_reply",
]

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 480  # same cap as the voice send_sms tool (about 3 segments)
# The chat holds at most this many texts (the new one included) from this
# long ago. Calls are not in it: the model reads them with thread_history.
CHAT_LIMIT = 20
CHAT_WINDOW = timedelta(hours=24)
# At most this many agent replies in one thread per REPLY_CAP_WINDOW (a rolling
# 24 hours, no reset when a person writes): past it the agent stays quiet until
# older replies leave the window. A loop breaker.
MAX_REPLIES_PER_THREAD = 20
REPLY_CAP_WINDOW = timedelta(hours=24)

# A worker that has not finished within this long is presumed dead.
CLAIM_LEASE = timedelta(minutes=2)
# Claims per text (the first try included) before it is marked failed.
MAX_ATTEMPTS = 3
RETRY_BACKOFF = timedelta(seconds=30)  # doubled after each failed attempt
# ``sms.metadata_["skipped_reason"]`` for a text skipped at claim because the
# org has no funds. Never requeued (only ``threads.ACTIVE_CALL_SKIP`` is).
INSUFFICIENT_FUNDS_SKIP = "insufficient_funds"

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
    # A text revived after a call counts its age from the requeue.
    age_from = func.coalesce(
        Sms.metadata_["requeued_at"].astext.cast(DateTime(timezone=True)),
        Sms.requested_at,
    )
    await db.execute(
        update(Sms).where(*base, age_from < cutoff).values(agent_reply_state="skipped")
    )
    await db.execute(
        update(Sms)
        .where(*base, Sms.agent_reply_attempts >= MAX_ATTEMPTS)
        .values(agent_reply_state="failed")
    )


async def prior_reply(db: AsyncSession, inbound: Sms) -> Sms | None:
    """The first agent reply row to ``inbound``, or None. The one lookup for
    "this text was already answered": reply-sms idempotency and the claim."""
    stmt = (
        select(Sms)
        .where(
            Sms.organization_id == inbound.organization_id,
            Sms.direction == "outbound",
            Sms.agent_id.is_not(None),
            Sms.to_e164 == inbound.from_e164,
            Sms.metadata_["reply_to_sms_id"].astext == str(inbound.id),
        )
        .order_by(Sms.requested_at)
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first()


async def claim_pending_reply(db: AsyncSession) -> ClaimedReply | None:
    """Claim the oldest answerable inbound text that still has an agent, or None.

    ``FOR UPDATE SKIP LOCKED`` lets several workers (and replicas) poll the
    same table. The claim sets ``processing`` with a lease and commits, so the
    row lock and the connection are free before the slow LLM and API calls. A
    crashed worker's lease runs out and the row is claimed again, up to
    ``MAX_ATTEMPTS`` claims. Texts older than
    ``HAIL_TEXT_REPLY_MAX_AGE_SECONDS`` are skipped, not answered late.
    A retried pending text whose reply was already sent is marked done. A
    pending text whose org has no funds (``has_funds``, the same rule as
    ``POST /internal/agent/reply-sms``) is skipped before any model call.
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
    # Orgs already read as unfunded in this call: a backlog of one org's texts
    # costs one ledger read, not one per text.
    unfunded: set[uuid.UUID] = set()
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
        if number is None or agent is None or not answers_texts(agent):
            # Routing changed between ingest and now: drop it quietly and take
            # the next row, so a backlog of dropped texts does not cost a poll
            # each.
            await finish_reply(db, sms, "skipped")
            continue
        org = sms.organization_id
        # Only pending rows: an expired lease may still have a live worker, so
        # it is reclaimed as before and reply-sms decides.
        pending = sms.agent_reply_state == "pending"
        if (
            pending
            and sms.agent_reply_attempts
            and await prior_reply(db, sms) is not None
        ):
            # An earlier attempt sent the reply but its POST timed out.
            await finish_reply(db, sms, "done")
            continue
        if pending and (org in unfunded or not await has_funds(db, org)):
            # reply-sms would refuse the send: skip before the model runs.
            unfunded.add(org)
            logger.info("sms_id=%s reply skipped: %s", sms.id, INSUFFICIENT_FUNDS_SKIP)
            sms.agent_reply_state = "skipped"
            sms.metadata_ = {
                **(sms.metadata_ or {}),
                "skipped_reason": INSUFFICIENT_FUNDS_SKIP,
            }
            await db.commit()
            continue
        sms.agent_reply_state = "processing"
        sms.agent_reply_attempts += 1
        sms.agent_reply_available_at = now + CLAIM_LEASE
        attempt = sms.agent_reply_attempts
        await db.commit()
        return ClaimedReply(sms=sms, agent=agent, number=number, attempt=attempt)


def _thread_filter(a: str, b: str):
    return or_(
        and_(Sms.from_e164 == a, Sms.to_e164 == b),
        and_(Sms.from_e164 == b, Sms.to_e164 == a),
    )


def thread_lock_key(sms: Sms) -> str:
    """Advisory-lock key for one thread: same for both directions."""
    a, b = sorted((sms.from_e164, sms.to_e164))
    return f"reply-sms:{sms.organization_id}:{a}:{b}"


async def thread_history_for_reply(
    db: AsyncSession, sms: Sms, agent: Agent
) -> list[ThreadItem]:
    """The last ``CHAT_LIMIT`` texts of the thread within ``CHAT_WINDOW`` up to
    and including ``sms``, oldest first. Texts only: no call turns."""
    # Texts sent through POST /sms (no agent) between this number pair belong
    # to the conversation the text agent is having, so it sees them too.
    items = await thread_items(
        db,
        sms.organization_id,
        agent.id,
        sms.from_e164,
        limit=CHAT_LIMIT,
        until=sms.requested_at,
        unassigned_pair=(sms.to_e164, sms.from_e164),
        source="sms",
        window=CHAT_WINDOW,
    )
    # The reply answers ``sms``: keep it last even when another item shares its
    # timestamp.
    key = f"sms:{sms.id}"
    current = next((i for i in items if i.id == key), None)
    if current is None:
        current = ThreadItem(id=key, at=sms.requested_at, kind="text_in", text=sms.body)
    earlier = [i for i in items if i.id != key]
    return earlier[-(CHAT_LIMIT - 1) :] + [current]


async def replies_in_thread(db: AsyncSession, sms: Sms) -> int:
    """Agent text replies (not voice send_sms rows) in this thread within ``REPLY_CAP_WINDOW``."""
    since = datetime.now(timezone.utc) - REPLY_CAP_WINDOW
    stmt = select(func.count(Sms.id)).where(
        Sms.organization_id == sms.organization_id,
        Sms.direction == "outbound",
        Sms.agent_id.is_not(None),
        Sms.metadata_["reply_to_sms_id"].astext.is_not(None),
        _thread_filter(sms.from_e164, sms.to_e164),
        Sms.requested_at >= since,
    )
    return int((await db.execute(stmt)).scalar_one() or 0)


def build_chat_messages(
    agent: Agent, history: list[ThreadItem]
) -> list[dict[str, Any]]:
    """OpenAI-style messages: system (preamble, the thread_history hint and
    the instructions) then the texts, inbound as ``user`` and outbound as
    ``assistant``. Call items are left out."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_text_instructions(agent.system_prompt, thread_tool=True),
        }
    ]
    for item in history:
        if item.kind not in ("text_in", "text_out"):
            continue
        role = "user" if item.kind == "text_in" else "assistant"
        messages.append({"role": role, "content": item.text})
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
