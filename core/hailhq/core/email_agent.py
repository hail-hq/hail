"""Email replies by an agent.

``ingest_inbound`` calls :func:`queue_decision` for each new row. A row that
passes every rule becomes ``agent_reply_state = 'pending'``. The voicebot's text
worker (``hailhq.voicebot.emailbot``) claims it, builds the chat from the agent's
instructions and the thread, asks the LLM for one reply, and sends it through
``POST /internal/agent/reply-email``. The route runs the send-time rules again.

Same claim, lease, retry, and thread cap as ``hailhq.core.text_agent``. This
module holds the DB side so the worker stays LLM-only.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Literal

from hailhq.core.billing import has_funds
from hailhq.core.config import settings
from hailhq.core.directory import is_verified_member
from hailhq.core.email_mime import (
    MAX_HEADER_CHARS,
    MAX_SUBJECT_CHARS,
    ParsedMime,
    reference_ids,
    reply_references,
    reply_subject,
)
from hailhq.core.forward_targets import normalize_address
from hailhq.core.models import Agent, Email, EmailDomain, Sms
from hailhq.core.prompts import (
    TEAM_EMAIL_LABEL,
    SenderTrust,
    build_email_instructions,
)
from hailhq.core.providers.email.ses import SES_NOT_ACCEPTED_FAILURES
from hailhq.core.text_agent import (
    CHAT_LIMIT,
    MAX_ATTEMPTS,
    MAX_REPLIES_PER_THREAD,
    REPLY_CAP_WINDOW,
    RETRY_BACKOFF,
)
from sqlalchemy import (
    ColumnElement,
    Text,
    and_,
    any_,
    cast,
    false,
    func,
    literal_column,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

logger = logging.getLogger("hailhq.core.email_agent")

__all__ = [
    "MAX_HEADER_CHARS",
    "MAX_REPLY_CHARS",
    "MAX_TOOL_SENDS",
    "ClaimedEmailReply",
    "EmailScope",
    "ReplyDecision",
    "answers_emails",
    "build_chat_messages",
    "claim_pending_reply",
    "email_history_filter",
    "email_thread",
    "finish_reply",
    "is_authenticated",
    "prior_reply",
    "proves_sender",
    "queue_decision",
    "replies_in_thread",
    "reply_references",
    "reply_subject",
    "retry_reply",
    "routed_agent",
    "run_refusal",
    "same_thread",
    "sender_refusal",
    "sender_trust",
    "stamp_skip",
    "thread_lock_key",
    "tool_sends_for_reply",
    "with_skip_reason",
]

MAX_REPLY_CHARS = 5000  # same cap as the send_email tool
# A worker that has not finished within this long is presumed dead. Longer than
# the text lease: one email run has a three-minute model/tool budget and
# the reply post, so a slow run must not be claimed again and paid twice.
EMAIL_CLAIM_LEASE = timedelta(minutes=5)
# Characters of one email in the chat. Quoted history makes bodies long.
MAX_EMAIL_CHARS = 6000
# HTML read per email, cut before parsing so a huge body costs a bounded time.
MAX_HTML_CHARS = 200_000
# Texts and emails one reply run may send, so one email cannot spend without limit.
MAX_TOOL_SENDS = 3
# Newest ids read from one email's headers when finding its thread. The sender
# writes them, and asyncpg takes at most 32767 query arguments.
MAX_REFERENCED_IDS = 100

ReplyState = Literal["done", "skipped", "failed"]

_NO_REPLY_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?)"
    r"([+.\-_].*)?$"
)
# Why a stored row stays out of the chat: nothing proved who wrote it, or it was
# never meant for the agent.
_AGENT_SEND_KEYS = ("reply_to_email_id", "inbound_email_id", "thread_sender")
_NOT_TEAM_SEND_KEYS = ("forwarded_from", "system_kind", "call_id", "tool_invocation_id")
_UNTRUSTED_SKIPS = (
    "auto_generated",
    "sender_blocked",
    "sender_not_member",
    "sender_not_verified",
    "spam",
    "virus",
)


@dataclass(frozen=True)
class ReplyDecision:
    agent: Agent
    state: Literal["pending", "skipped"]
    reason: str | None = None


@dataclass(frozen=True)
class ClaimedEmailReply:
    email: Email
    agent: Agent
    domain: EmailDomain
    attempt: int  # which claim this is, 1-based


def answers_emails(agent: Agent | None) -> bool:
    """True for a live agent with ``email_enabled``: the one test for queueing,
    claiming, and sending an email reply."""
    return agent is not None and agent.status == "live" and agent.email_enabled


async def routed_agent(
    db: AsyncSession, email: Email
) -> tuple[EmailDomain, Agent] | None:
    """The address of this inbound row and the agent that answers it now, or
    None when either is gone or the agent stopped answering email."""
    if email.email_domain_id is None:
        return None
    domain = await db.get(EmailDomain, email.email_domain_id)
    if domain is None or domain.email_agent_id is None:
        return None
    if domain.kind == "custom" and (
        domain.verification_status != "verified" or not domain.inbound_enabled
    ):
        return None
    agent = await db.get(Agent, domain.email_agent_id)
    if not answers_emails(agent):
        return None
    assert agent is not None
    return domain, agent


def proves_sender(parsed: ParsedMime, dmarc_verdict: str | None) -> bool:
    """One well-formed ``From`` address and a DMARC ``PASS``. Ingest stores the
    answer in ``emails.from_verified``."""
    return parsed.from_count == 1 and parsed.from_clean and dmarc_verdict == "PASS"


def is_authenticated(email: Email) -> bool:
    """True when ingest proved the ``From`` address. Tools and thread history
    need it, under every scope."""
    return email.from_verified is True


def with_skip_reason(reason: str) -> ColumnElement[Any]:
    """``metadata`` with this skip reason merged in. ``||`` on a JSON ``null``
    makes an array and the key is lost, so that row starts from ``{}``."""
    current = func.coalesce(
        func.nullif(Email.metadata_, literal_column("'null'::jsonb")),
        literal_column("'{}'::jsonb"),
    )
    return current.op("||")(func.jsonb_build_object("agent_skip_reason", reason))


def _sender_blocked(sender: str, base_domain: str) -> bool:
    """True for an address that must never get a reply: a daemon, a noreply
    box, or Hail itself. These two checks stop every loop: a hail-mail reply
    comes from the base domain and a custom-domain reply from ``noreply@``.
    A custom domain's host is not checked: the team's members send from it,
    and a member who Cc's themselves still gets a reply."""
    local, _, host = sender.rpartition("@")
    if not local or not host:
        return True
    if _NO_REPLY_LOCAL.match(local):
        return True
    return bool(base_domain) and host == base_domain.lower()


async def sender_refusal(
    db: AsyncSession, domain: EmailDomain, *, sender: str, verified: bool
) -> str | None:
    """Why this sender may not reach the agent, or None. Fails closed.

    DMARC ``PASS`` is the only proof of a ``From`` address. SES reports ``GRAY``
    when any SPF or DKIM check passed for a domain with no policy or ``p=none``,
    aligned or not, so a forger with a valid signature of their own gets
    ``GRAY``. Unproven mail gets no reply under any scope: a reply to a forged
    ``From`` would reach an innocent third party.
    """
    if not verified:
        return "sender_not_verified"
    if domain.email_agent_scope == "anyone":
        return None
    if not await is_verified_member(db, domain.organization_id, sender):
        return "sender_not_member"
    return None


def _may_have_reached_someone():
    """Any email row but one SES proved it never took."""
    return or_(
        Email.status != "failed",
        Email.end_reason.is_(None),
        Email.end_reason.not_in(SES_NOT_ACCEPTED_FAILURES),
    )


async def replies_in_thread(
    db: AsyncSession,
    organization_id: uuid.UUID,
    sender: str,
    *,
    excluding_run: uuid.UUID | None = None,
) -> int:
    """Agent sends for this sender within ``REPLY_CAP_WINDOW``: replies, and
    tool sends in any channel. A run that only uses tools still counts.
    ``excluding_run`` leaves out the tool sends of that inbound email's run.
    Every reply row counts, because a later attempt can send a throttled one.
    A tool email SES never took stays out: no attempt sends it again."""
    since = datetime.now(timezone.utc) - REPLY_CAP_WINDOW

    def not_this_run(model: type[Email | Sms]):
        if excluding_run is None:
            return true()
        run = model.metadata_["inbound_email_id"].astext
        return run.is_distinct_from(str(excluding_run))

    emails = select(func.count(Email.id)).where(
        Email.organization_id == organization_id,
        Email.direction == "outbound",
        Email.agent_id.is_not(None),
        Email.requested_at >= since,
        or_(
            and_(
                Email.metadata_["reply_to_email_id"].astext.is_not(None),
                sender == any_(Email.to_addresses),
            ),
            and_(
                Email.metadata_["thread_sender"].astext == sender,
                not_this_run(Email),
                _may_have_reached_someone(),
            ),
        ),
    )
    texts = select(func.count(Sms.id)).where(
        Sms.organization_id == organization_id,
        Sms.direction == "outbound",
        Sms.agent_id.is_not(None),
        Sms.requested_at >= since,
        Sms.metadata_["thread_sender"].astext == sender,
        not_this_run(Sms),
    )
    total = int((await db.execute(emails)).scalar_one() or 0)
    return total + int((await db.execute(texts)).scalar_one() or 0)


async def prior_reply(db: AsyncSession, email: Email) -> Email | None:
    """The reply the agent already wrote for this inbound row, or None. Its
    existence means a retry: the row was under the cap when the reply was
    written. Only an agent's row to this sender counts."""
    stmt = (
        select(Email)
        .where(
            Email.organization_id == email.organization_id,
            Email.direction == "outbound",
            Email.agent_id.is_not(None),
            Email.metadata_["reply_to_email_id"].astext == str(email.id),
            Email.to_addresses == [normalize_address(email.from_address)],
        )
        .order_by(Email.requested_at)
    )
    return (await db.execute(stmt)).scalars().first()


async def run_refusal(
    db: AsyncSession,
    domain: EmailDomain,
    email: Email,
    *,
    check_cap: bool = True,
    count_own_sends: bool = False,
) -> str | None:
    """Why this row must not be answered now, or None. Scope, cap, and funds
    can change between ingest and a claim, and the model costs money.

    ``check_cap`` is off when the reply already exists and only needs sending.
    The run's own tool sends do not stop its one reply, so they count only with
    ``count_own_sends``: the tool routes use it, and a tool send cannot take the
    last slot twice.
    """
    sender = normalize_address(email.from_address)
    refusal = await sender_refusal(
        db, domain, sender=sender, verified=is_authenticated(email)
    )
    if refusal is not None:
        return refusal
    if check_cap:
        count = await replies_in_thread(
            db,
            email.organization_id,
            sender,
            excluding_run=None if count_own_sends else email.id,
        )
        if count >= MAX_REPLIES_PER_THREAD:
            return "thread_cap"
    if not await has_funds(db, email.organization_id):
        return "insufficient_funds"
    return None


async def sender_trust(db: AsyncSession, email: Email) -> SenderTrust:
    """What the prompt may say about a proven sender."""
    sender = normalize_address(email.from_address)
    member = await is_verified_member(db, email.organization_id, sender)
    return "member" if member else "outside"


async def queue_decision(
    db: AsyncSession,
    domain: EmailDomain,
    *,
    parsed: ParsedMime,
    envelope_from: str,
    dmarc_verdict: str | None,
    suppress: str | None,
    over_cap: bool,
    funded: bool,
    base_domain: str,
) -> ReplyDecision | None:
    """What to do with a new inbound row. None when no agent answers this
    address. The first failed rule names the skip reason; funds come last, so
    the out-of-credit alert means a reply was really missed."""
    if domain.email_agent_id is None:
        return None
    agent = await db.get(Agent, domain.email_agent_id)
    if not answers_emails(agent):
        return None
    assert agent is not None

    def skip(reason: str) -> ReplyDecision:
        return ReplyDecision(agent=agent, state="skipped", reason=reason)

    if suppress is not None:
        return skip(suppress)
    if over_cap:
        return skip("inbound_rate_limit")
    if parsed.auto_generated or not envelope_from:
        return skip("auto_generated")
    sender = normalize_address(parsed.from_address or "")
    if _sender_blocked(sender, base_domain):
        return skip("sender_blocked")
    refusal = await sender_refusal(
        db, domain, sender=sender, verified=proves_sender(parsed, dmarc_verdict)
    )
    if refusal is not None:
        return skip(refusal)
    count = await replies_in_thread(db, domain.organization_id, sender)
    if count >= MAX_REPLIES_PER_THREAD:
        return skip("thread_cap")
    if not funded:
        return skip("insufficient_funds")
    return ReplyDecision(agent=agent, state="pending")


async def _expire_unclaimable(db: AsyncSession, now: datetime) -> None:
    """Close rows that must not be answered: too old, or out of attempts."""
    base = (
        Email.direction == "inbound",
        or_(
            Email.agent_reply_state == "pending",
            and_(
                Email.agent_reply_state == "processing",
                Email.agent_reply_available_at <= now,
            ),
        ),
    )
    cutoff = now - timedelta(seconds=settings.hail_text_reply_max_age_seconds)
    await db.execute(
        update(Email)
        .where(*base, Email.requested_at < cutoff)
        .values(
            agent_reply_state="skipped",
            metadata_=with_skip_reason("expired"),
        )
    )
    await db.execute(
        update(Email)
        .where(*base, Email.agent_reply_attempts >= MAX_ATTEMPTS)
        .values(agent_reply_state="failed")
    )
    # Nothing retries a reply this old, so none may stay queued forever.
    await db.execute(
        update(Email)
        .where(
            Email.direction == "outbound",
            Email.status == "queued",
            Email.agent_id.is_not(None),
            or_(
                Email.metadata_["reply_to_email_id"].astext.is_not(None),
                Email.metadata_["inbound_email_id"].astext.is_not(None),
            ),
            Email.requested_at < cutoff,
        )
        .values(status="failed", end_reason="expired", failed_at=now)
    )


async def claim_pending_reply(db: AsyncSession) -> ClaimedEmailReply | None:
    """Claim the oldest answerable inbound email that still has an agent, or
    None. Same locking and lease rules as ``text_agent.claim_pending_reply``."""
    now = datetime.now(timezone.utc)
    await _expire_unclaimable(db, now)
    stmt = (
        select(Email)
        .where(
            Email.direction == "inbound",
            or_(
                and_(
                    Email.agent_reply_state == "pending",
                    or_(
                        Email.agent_reply_available_at.is_(None),
                        Email.agent_reply_available_at <= now,
                    ),
                ),
                and_(
                    Email.agent_reply_state == "processing",
                    Email.agent_reply_available_at <= now,
                ),
            ),
        )
        .order_by(Email.requested_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    while True:
        email = (await db.execute(stmt)).scalar_one_or_none()
        if email is None:
            await db.commit()  # keep the expiry updates
            return None
        routed = await routed_agent(db, email)
        if routed is not None:
            domain, agent = routed
            # The route may have moved to another agent since ingest.
            email.agent_id = agent.id
            email.agent_reply_state = "processing"
            email.agent_reply_attempts += 1
            email.agent_reply_available_at = now + EMAIL_CLAIM_LEASE
            attempt = email.agent_reply_attempts
            await db.commit()
            return ClaimedEmailReply(
                email=email, agent=agent, domain=domain, attempt=attempt
            )
        # Routing changed between ingest and now: drop it and take the next row.
        await finish_reply(db, email, "skipped", reason="no_agent")


async def stamp_skip(db: AsyncSession, email_id: uuid.UUID, reason: str) -> None:
    """Say why a row got no reply. A sender refusal also keeps the row out of
    later chats (``_trusted_inbound``), and no funds raises the console's
    out-of-credit alert. A reply left queued will not be retried now, so it is
    marked failed; a send still running overwrites that with its result."""
    row = await db.get(Email, email_id)
    if row is None or row.direction != "inbound":
        return
    meta = dict(row.metadata_ or {})
    meta["agent_skip_reason"] = reason
    if reason == "insufficient_funds":
        reasons = list(meta.get("suppressed_reasons") or [])
        if reason not in reasons:
            reasons.append(reason)
        meta["suppressed_reasons"] = reasons
    row.metadata_ = meta
    prior = await prior_reply(db, row)
    if prior is not None and prior.status == "queued":
        await db.execute(
            update(Email)
            .where(
                Email.id == prior.id,
                Email.status == "queued",
                Email.requested_at == prior.requested_at,
            )
            .values(
                status="failed",
                end_reason=reason,
                failed_at=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )
    await db.commit()


async def finish_reply(
    db: AsyncSession,
    email: Email,
    state: ReplyState,
    *,
    attempt: int | None = None,
    reason: str | None = None,
) -> None:
    """Record the final state, and with ``reason`` why it was skipped. With
    ``attempt`` it applies only while that claim still owns the row."""
    stmt = update(Email).where(Email.id == email.id)
    if attempt is not None:
        stmt = stmt.where(
            Email.agent_reply_state == "processing",
            Email.agent_reply_attempts == attempt,
        )
    result = await db.execute(stmt.values(agent_reply_state=state))
    await db.commit()
    if reason is not None and result.rowcount:  # type: ignore[attr-defined]
        await stamp_skip(db, email.id, reason)


async def retry_reply(
    db: AsyncSession, claimed: ClaimedEmailReply, *, delay: timedelta | None = None
) -> str | None:
    """Back to ``pending`` after a backoff, or ``failed`` once ``MAX_ATTEMPTS``
    claims are used. None when the claim lost the row.

    ``delay`` means an earlier send may still be running, so nothing failed: the
    row waits that long, and this claim does not count as an attempt.
    """
    waiting = delay is not None
    if not waiting and claimed.attempt >= MAX_ATTEMPTS:
        await finish_reply(db, claimed.email, "failed", attempt=claimed.attempt)
        return "failed"
    wait = delay or RETRY_BACKOFF * 2 ** (claimed.attempt - 1)
    result = await db.execute(
        update(Email)
        .where(
            Email.id == claimed.email.id,
            Email.agent_reply_state == "processing",
            Email.agent_reply_attempts == claimed.attempt,
        )
        .values(
            agent_reply_state="pending",
            agent_reply_available_at=datetime.now(timezone.utc) + wait,
            agent_reply_attempts=claimed.attempt - 1 if waiting else claimed.attempt,
        )
    )
    await db.commit()
    return "pending" if result.rowcount else None  # type: ignore[attr-defined]


def thread_lock_key(email: Email) -> str:
    """Advisory-lock key for this sender's thread."""
    return (
        f"reply-email:{email.organization_id}:{normalize_address(email.from_address)}"
    )


def _referenced_ids(email: Email) -> list[str]:
    ids = list(email.references_ids or [])[-MAX_REFERENCED_IDS:]
    if email.in_reply_to:
        ids.append(email.in_reply_to.strip())
    return ids


def _provider_message_ids(message_ids: set[str]) -> set[str]:
    """SES overrides ``Message-ID`` on outbound mail with its own id at an SES
    host, so a referenced reply is found through ``provider_message_id``."""
    return {message_id.strip("<> ").partition("@")[0] for message_id in message_ids}


def _virus_safe(model=Email):
    return or_(
        model.virus_verdict.is_(None),
        func.upper(model.virus_verdict).not_in(("FAIL", "FAILED")),
    )


def _trusted_inbound(model=Email):
    """Inbound rows the chat may quote. ``from_verified`` is the allowlist: a
    forged row with the same ``From`` stays out, and so does any row ingest did
    not prove. The other checks only take rows away."""
    return and_(
        model.direction == "inbound",
        model.from_verified.is_(True),
        or_(model.spam_verdict.is_(None), model.spam_verdict != "FAIL"),
        _virus_safe(model),
        or_(
            model.metadata_["agent_skip_reason"].astext.is_(None),
            model.metadata_["agent_skip_reason"].astext.not_in(_UNTRUSTED_SKIPS),
        ),
    )


def _agent_wrote(row: Email) -> bool:
    """An agent's outbound row. Deleting the agent clears ``agent_id``, so the
    metadata counts too."""
    meta = row.metadata_ or {}
    return row.agent_id is not None or any(key in meta for key in _AGENT_SEND_KEYS)


def email_history_filter(sender: str, *, allow_call_mail: bool = False):
    """The same sender-proof and provenance rules as thread history, in SQL.

    Search applies this before LIMIT so excluded mail cannot hide valid matches.
    Caller queries still own organization, inbox and peer boundaries.
    """

    def has_key(key):
        return func.coalesce(Email.metadata_.has_key(key), false())

    agent_wrote = or_(
        Email.agent_id.is_not(None), *(has_key(k) for k in _AGENT_SEND_KEYS)
    )
    parent = aliased(Email)
    parent_id = func.coalesce(
        Email.metadata_["reply_to_email_id"].astext,
        Email.metadata_["inbound_email_id"].astext,
    )
    trusted_parent = (
        select(parent.id)
        .where(
            cast(parent.id, Text) == parent_id,
            parent.organization_id == Email.organization_id,
            parent.email_domain_id == Email.email_domain_id,
            _trusted_inbound(parent),
            func.lower(parent.from_address) == sender,
        )
        .correlate(Email)
        .exists()
    )
    team_send = and_(~agent_wrote, *(~has_key(k) for k in _NOT_TEAM_SEND_KEYS))
    if allow_call_mail:
        # Browser and outbound voice scopes already fix both mailbox and peer.
        # Their own call sends have no inbound email parent to prove.
        team_send = or_(
            team_send,
            and_(
                ~agent_wrote,
                Email.metadata_["call_id"].astext.is_not(None),
                ~has_key("forwarded_from"),
                ~has_key("system_kind"),
            ),
        )
    return and_(
        _virus_safe(),
        or_(
            _trusted_inbound(),
            and_(
                Email.direction == "outbound",
                or_(and_(agent_wrote, trusted_parent), team_send),
            ),
        ),
    )


async def email_thread(db: AsyncSession, email: Email) -> list[Email]:
    """The thread up to and including ``email``, oldest first, at most
    ``CHAT_LIMIT`` rows.

    No reply goes to an unproven sender, and this stays closed for one anyway. A
    proven sender gets rows with this sender only, because ``References`` is
    written by the sender and could name someone else's message.
    """
    if not is_authenticated(email):
        return [email]
    sender = normalize_address(email.from_address)
    seen: dict[uuid.UUID, Email] = {email.id: email}
    frontier = [email]
    while frontier and len(seen) < CHAT_LIMIT:
        ids = {i for row in frontier for i in _referenced_ids(row)}
        if not ids:
            break
        stmt = select(Email).where(
            Email.organization_id == email.organization_id,
            Email.email_domain_id == email.email_domain_id,
            or_(
                Email.message_id.in_(ids),
                Email.provider_message_id.in_(_provider_message_ids(ids)),
            ),
            email_history_filter(sender),
            or_(
                and_(
                    Email.direction == "inbound",
                    func.lower(Email.from_address) == sender,
                ),
                and_(Email.direction == "outbound", sender == any_(Email.to_addresses)),
            ),
        )
        # Newest first, so a long thread keeps its recent mail.
        newest = stmt.order_by(Email.requested_at.desc()).limit(CHAT_LIMIT)
        rows = (await db.execute(newest)).scalars().all()
        frontier = [r for r in rows if r.id not in seen]
        seen.update({r.id: r for r in frontier})
    earlier = [row for row in seen.values() if row.id != email.id]
    earlier.sort(key=lambda r: r.requested_at)
    return earlier[-(CHAT_LIMIT - 1) :] + [email]


class _TextExtractor(HTMLParser):
    """The visible text of an HTML body."""

    _HIDDEN = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._HIDDEN:
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._HIDDEN and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _html_text(body_html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(body_html[:MAX_HTML_CHARS])
        parser.close()
    except (AssertionError, ValueError):
        # Sender-written markup must not fail the reply. Keep what was read.
        logger.warning("email html could not be parsed to the end")
    return " ".join(" ".join(parser.parts).split())


def _plain_body(email: Email) -> str:
    body = email.body_text
    if not (body or "").strip() and email.body_html:
        body = _html_text(email.body_html)
    return (body or "").strip()[:MAX_EMAIL_CHARS]


def build_chat_messages(
    agent: Agent,
    thread: list[Email],
    *,
    trust: SenderTrust,
    sends_tried: bool = False,
) -> list[dict[str, Any]]:
    """OpenAI-style messages: the system message, then the thread. Mail from
    people is ``user``, labeled so the model reads it as mail, and only an
    agent's own replies are ``assistant``. ``sends_tried`` means an earlier
    attempt at this reply used the send tools, and the chat does not show them."""
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": build_email_instructions(
                agent.system_prompt, trust=trust, sends_tried=sends_tried
            ),
        }
    ]
    for row in thread:
        if row.direction == "inbound":
            text = (
                f"Email from {row.from_address}\n"
                f"Subject: {(row.subject or '')[:MAX_SUBJECT_CHARS]}\n\n{_plain_body(row)}"
            )
            messages.append({"role": "user", "content": text})
        elif _agent_wrote(row):
            messages.append({"role": "assistant", "content": _plain_body(row)})
        else:
            text = f"{TEAM_EMAIL_LABEL}\nSubject: {(row.subject or '')[:MAX_SUBJECT_CHARS]}\n\n{_plain_body(row)}"
            messages.append({"role": "user", "content": text})
    return messages


async def tool_sends_for_reply(db: AsyncSession, inbound: Email) -> int:
    """Emails and texts this reply run sent with its tools. An email that SES
    proved it never took stays out: it reached nobody."""
    total = 0
    for model in (Email, Sms):
        stmt = select(func.count(model.id)).where(
            model.organization_id == inbound.organization_id,
            model.direction == "outbound",
            model.agent_id.is_not(None),
            model.metadata_["inbound_email_id"].astext == str(inbound.id),
        )
        if model is Email:
            stmt = stmt.where(_may_have_reached_someone())
        total += int((await db.execute(stmt)).scalar_one() or 0)
    return total


@dataclass(frozen=True)
class EmailScope:
    """Server-selected peer and mailbox, with shared history trust rules."""

    organization_id: uuid.UUID
    peer: str
    inbox: str
    email_ids: tuple[uuid.UUID, ...] | None = None
    email_domain_id: uuid.UUID | None = None

    def query(self):
        query = select(Email).where(
            Email.organization_id == self.organization_id,
        )
        if self.email_domain_id is not None:
            query = query.where(Email.email_domain_id == self.email_domain_id)
        query = query.where(
            email_history_filter(
                self.peer.lower(), allow_call_mail=self.email_domain_id is None
            )
        )
        if self.email_ids is not None:
            query = query.where(Email.id.in_(self.email_ids))
        incoming = [
            Email.direction == "inbound",
            func.lower(Email.from_address) == self.peer.lower(),
        ]
        outgoing = [
            Email.direction == "outbound",
            Email.to_addresses.any(self.peer),
        ]
        if self.email_domain_id is None:
            incoming.append(Email.to_addresses.any(self.inbox))
            outgoing.append(func.lower(Email.from_address) == self.inbox.lower())
        return query.where(
            or_(
                and_(*incoming),
                and_(*outgoing),
            )
        )


def same_thread(first: Email, second: Email) -> bool:
    """Compare stored chains after the caller has enforced peer and inbox scope.

    Keep every valid reference for old-file reuse; outgoing header limits do not
    constrain which older message a stored chain may refer to.
    """
    first_ids = set(reference_ids(first, limit=None))
    second_ids = set(reference_ids(second, limit=None))
    return (
        first.id == second.id
        or bool(first_ids & second_ids)
        or any(
            source.direction == "outbound"
            and source.provider_message_id in _provider_message_ids(references)
            for source, references in ((first, second_ids), (second, first_ids))
        )
    )
