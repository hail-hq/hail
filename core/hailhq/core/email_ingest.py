"""Inbound persistence service.

Orchestrates: fetch raw MIME from S3 → parse → resolve owning domain per
recipient → write one Email row per matched dedup scope → extract attachments
into S3 and write email_attachments rows. Idempotency is enforced by
kind-aware partial unique indexes — hail_mail dedupes per
``(organization_id, message_id)`` while custom dedupes per
``(email_domain_id, message_id)``. A duplicate raises IntegrityError, which we
absorb and short-circuit to the existing row's id. Mail without a Message-ID
header falls back to the matching ``provider_message_id`` index — the SES
receipt id, which is what repeats on redelivery.

Forwarding and webhook fan-out are NOT triggered here. The ingest
service only persists. The caller (the API endpoint) layers on top.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import cast, func, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.email_forwarding import LoopDetected, build_forwarded, detect_loop
from hailhq.core.email_mime import ParsedAttachment, ParsedMime, parse_mime
from hailhq.core.email_routing import classify_hail_mail_recipient
from hailhq.core.forward_limiter import ForwardLimiter
from hailhq.core.models import Email, EmailAttachment, EmailDomain
from hailhq.core.providers.email.inbound.base import InboundMessage
from hailhq.core.s3_inbound import S3InboundClient
from hailhq.core.urls import join_url
from hailhq.core.webhook_fanout import build_event_data

__all__ = ["FanoutFn", "ForwardEnqueue", "FundsCheck", "IngestResult", "ingest_inbound"]


ForwardEnqueue = Callable[..., Awaitable[None]]
FanoutFn = Callable[..., Awaitable[int]]
FundsCheck = Callable[[AsyncSession, UUID], Awaitable[bool]]

# Inbound dedup indexes whose unique violation is a benign concurrent-delivery
# race (absorbed, not raised). Mirrors the four partial indexes defined in
# ``models.Email.__table_args__``.
_BENIGN_DEDUP_INDEXES = frozenset(
    {
        "emails_hailmail_inbound_message_id_uq",
        "emails_custom_inbound_message_id_uq",
        "emails_hailmail_inbound_pmid_uq",
        "emails_custom_inbound_pmid_uq",
    }
)


@dataclass
class IngestResult:
    email_ids: list[UUID] = field(default_factory=list)
    created_email_ids: list[tuple[UUID, UUID]] = field(default_factory=list)
    suppressed_reasons: list[str] = field(default_factory=list)
    skipped_recipients: list[str] = field(default_factory=list)


def _suppress_reason(message: InboundMessage) -> str | None:
    if message.virus_verdict == "FAIL":
        return "virus"
    if message.spam_verdict == "FAIL":
        return "spam"
    return None


async def _find_domain_for_recipient(
    db: AsyncSession, recipient: str, base_domain: str
) -> EmailDomain | None:
    classified = classify_hail_mail_recipient(recipient, base_domain)
    if classified is not None:
        stmt = (
            select(EmailDomain)
            .where(EmailDomain.kind == "hail_mail")
            .where(EmailDomain.local_prefix_user == classified.user_prefix)
            .where(EmailDomain.local_prefix_org == classified.org_prefix)
            .limit(1)
        )
        row = (await db.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row

    # Custom domains: match any local-part at a verified, inbound-enabled
    # custom domain. Receiving is opt-in per customer, so (unlike hail-mail)
    # both the verified and inbound_enabled gates are required.
    _, _, dom = recipient.partition("@")
    if not dom:
        return None
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.kind == "custom")
        .where(EmailDomain.domain == dom.lower())
        .where(EmailDomain.inbound_enabled.is_(True))
        .where(EmailDomain.verification_status == "verified")
        .order_by(EmailDomain.created_at.asc(), EmailDomain.id.asc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _persist_attachments(
    db: AsyncSession,
    *,
    email_id: UUID,
    attachments: list[ParsedAttachment],
    s3: S3InboundClient,
) -> None:
    for parsed in attachments:
        att_id = uuid4()
        key = f"attachments/{email_id}/{att_id}"
        await s3.put_attachment(key, parsed.payload, parsed.content_type)
        row = EmailAttachment(
            id=att_id,
            email_id=email_id,
            filename=parsed.filename,
            content_type=parsed.content_type,
            size_bytes=len(parsed.payload),
            s3_key=key,
            content_id=parsed.content_id,
        )
        db.add(row)


async def _existing_inbound_id(
    db: AsyncSession,
    domain: EmailDomain,
    message_id: str | None,
    provider_message_id: str | None,
) -> UUID | None:
    """Return the id of an existing inbound Email that matches this delivery.

    Dedup scope is kind-aware:
    - ``hail_mail``: org-scoped (one row per org, regardless of how many
      recipients share the same org). ``email_domain_kind`` is filtered too
      because ``organization_id`` alone isn't unique across kinds.
    - ``custom``: domain-scoped (one row per receiving domain, so a message
      to two custom domains in the same org produces two rows). ``email_domain_id``
      already pins the kind, so no extra filter is needed.
    """
    is_custom = domain.kind == "custom"
    if is_custom:
        scope = [Email.email_domain_id == domain.id]
    else:
        scope = [
            Email.organization_id == domain.organization_id,
            Email.email_domain_kind == "hail_mail",
        ]

    for column, value in (
        (Email.message_id, message_id),
        (Email.provider_message_id, provider_message_id),
    ):
        if value is None:
            continue
        stmt = select(Email.id).where(
            *scope, column == value, Email.direction == "inbound"
        )
        found = (await db.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
    return None


async def _persist_one(
    db: AsyncSession,
    *,
    parsed: ParsedMime,
    message: InboundMessage,
    domain: EmailDomain,
    suppress: str | None,
    s3: S3InboundClient,
) -> tuple[UUID | None, bool]:
    """Persist one inbound Email row; returns ``(email_id, created)``.

    ``created`` is False when the row already existed (SES at-least-once
    redelivery or a lost concurrent-insert race) — callers must skip side
    effects (forwarding, webhook fan-out) in that case.
    """
    # Idempotency: short-circuit if a matching inbound row already exists.
    # Scope is kind-aware (org-scoped for hail_mail, domain-scoped for
    # custom). A concurrent insert that slips past this SELECT is caught by
    # the SAVEPOINT-wrapped flush below.
    existing_id = await _existing_inbound_id(
        db, domain, parsed.message_id, message.provider_message_id
    )
    if existing_id is not None:
        return existing_id, False

    metadata: dict[str, str] = {}
    if suppress:
        metadata["suppressed"] = suppress

    # Real mail can be body-less (attachment-only, calendar-only). The
    # emails_body_required CHECK predates inbound; coalesce to "" rather
    # than dropping the message.
    body_text = parsed.body_text
    if body_text is None and parsed.body_html is None:
        body_text = ""

    email = Email(
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        email_domain_kind=domain.kind,
        direction="inbound",
        from_address=parsed.from_address or message.envelope_from,
        to_addresses=parsed.to_addresses or list(message.envelope_recipients),
        cc_addresses=parsed.cc_addresses or None,
        subject=parsed.subject or "",
        body_text=body_text,
        body_html=parsed.body_html,
        status="received",
        provider="ses",
        provider_message_id=message.provider_message_id,
        message_id=parsed.message_id,
        in_reply_to=parsed.in_reply_to,
        references_ids=parsed.references_ids,
        raw_s3_key=message.raw_s3_key,
        spam_verdict=message.spam_verdict,
        virus_verdict=message.virus_verdict,
        spf_verdict=message.spf_verdict,
        dkim_verdict=message.dkim_verdict,
        dmarc_verdict=message.dmarc_verdict,
        provider_received_at=message.received_at,
        metadata_=metadata,
    )
    # SAVEPOINT must be started before db.add() so SQLAlchemy's
    # _take_snapshot() flush does not fire on pending objects before the
    # SAVEPOINT is established.
    nested = await db.begin_nested()  # SAVEPOINT — local to this row
    db.add(email)
    try:
        await db.flush()
        await nested.commit()
    except IntegrityError as exc:
        await nested.rollback()
        # Only the dedupe indexes are a benign race; everything else (CHECK,
        # FK, NOT NULL violations) must propagate, not silently skip.
        exc_str = str(exc.orig)
        if not any(idx in exc_str for idx in _BENIGN_DEDUP_INDEXES):
            raise
        # A concurrent delivery won the race on one of the dedupe indexes.
        # Rolling back to the savepoint restores the outer transaction; the
        # other domains' rows in the same ingest_inbound batch are intact.
        existing_id = await _existing_inbound_id(
            db, domain, parsed.message_id, message.provider_message_id
        )
        return existing_id, False

    if suppress is None:
        await _persist_attachments(
            db,
            email_id=email.id,
            attachments=parsed.attachments,
            s3=s3,
        )
        await db.flush()
    return email.id, True


def _incoming_forward_hops(raw: bytes) -> int:
    """Parse ``X-Hail-Forward-Hops`` off the raw MIME, defaulting to 0."""
    msg = message_from_bytes(raw)
    raw_value = msg.get("X-Hail-Forward-Hops")
    if raw_value is None:
        return 0
    try:
        return int(raw_value.strip())
    except (ValueError, AttributeError):
        return 0


async def _enqueue_forwards(
    db: AsyncSession,
    *,
    domain: EmailDomain,
    parsed: ParsedMime,
    inbound_id: UUID,
    hops: int,
    hail_mail_base_domain: str,
    forward_max_hops: int,
    forward_default_per_hour: int,
    forward_enqueue: ForwardEnqueue,
) -> list[str]:
    """Enqueue forwards for one inbound row; returns this row's suppression
    reasons (``forward_rate_limit`` / ``forward_loop``) instead of mutating
    shared state — the caller folds them into the result and emits
    ``email.received.suppressed`` events."""
    targets = list(domain.forward_to or [])
    if not targets:
        return []

    reasons: list[str] = []
    limiter = ForwardLimiter(default_per_hour=forward_default_per_hour)
    # Soft cap: checked once per message, then we enqueue len(targets) sends —
    # can overshoot by N-1 on a multi-target domain. Acceptable for a soft cap.
    if not await limiter.can_forward(
        db,
        organization_id=domain.organization_id,
        email_domain_id=domain.id,
        override=domain.forward_rate_per_hour,
    ):
        return ["forward_rate_limit"]

    forwarder_address = f"forwarder+{domain.local_prefix_org}@{hail_mail_base_domain}"
    for target in targets:
        try:
            detect_loop(
                target=target,
                hops=hops,
                base_domain=hail_mail_base_domain,
                max_hops=forward_max_hops,
            )
        except LoopDetected as exc:
            if "forward_loop" not in reasons:
                reasons.append("forward_loop")
            if exc.cause == "hop_cap":
                return reasons
            continue
        forwarded = build_forwarded(
            parsed=parsed,
            target=target,
            forwarder_address=forwarder_address,
            inbound_id=inbound_id,
            hops=hops,
        )
        await forward_enqueue(
            db,
            organization_id=domain.organization_id,
            email_domain_id=domain.id,
            from_address=forwarded.from_address,
            to=forwarded.to_addresses[0],
            reply_to=forwarded.reply_to,
            subject=forwarded.subject,
            body_text=forwarded.body_text,
            body_html=forwarded.body_html,
            headers=forwarded.headers,
            inbound_id=inbound_id,
        )
    return reasons


async def _org_over_inbound_cap(
    db: AsyncSession,
    *,
    organization_id: UUID,
    cap: int,
) -> bool:
    """Return True when the org has reached or exceeded the inbound cap.

    A cap of 0 or negative is treated conservatively as "always over".
    """
    if cap <= 0:
        return True
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = (
        select(func.count())
        .select_from(Email)
        .where(Email.organization_id == organization_id)
        .where(Email.direction == "inbound")
        .where(Email.created_at >= since)
    )
    used = (await db.execute(stmt)).scalar_one()
    return used >= cap


async def ingest_inbound(
    db: AsyncSession,
    *,
    message: InboundMessage,
    s3: S3InboundClient,
    hail_mail_base_domain: str,
    forward_enqueue: ForwardEnqueue | None = None,
    forward_max_hops: int = 3,
    forward_default_per_hour: int = 200,
    fanout: FanoutFn | None = None,
    funds_check: FundsCheck | None = None,
    api_base_url: str | None = None,
    org_rate_per_hour: int,
) -> IngestResult:
    """Persist one InboundMessage; optionally enqueue forwarding sends.

    The ``forward_enqueue`` callback is invoked once per forward target on
    each newly-created, non-suppressed inbound row. Redeliveries (SES is
    at-least-once) match an existing row and fire no side effects. If the
    callback is absent, no forwarding fires (useful for ingest-only
    contexts and unit tests).
    """
    result = IngestResult()
    raw = await s3.fetch_raw(message.raw_s3_key)
    parsed = parse_mime(raw)
    suppress = _suppress_reason(message)
    if suppress:
        result.suppressed_reasons.append(suppress)
    # Hop counting trusts the X-Hail-Forward-Hops header; an external sender
    # can spoof it to suppress a tenant's forwarding (inherent to header-based
    # loop prevention — same trade-off as classic Received: counting).
    inbound_hops = _incoming_forward_hops(raw)

    # One row per dedup scope: custom → receiving domain id, hail_mail → org id.
    # (domain ids and org ids are disjoint UUID spaces, so one set is safe.)
    seen_scopes: set[UUID] = set()
    for recipient in message.envelope_recipients:
        domain = await _find_domain_for_recipient(db, recipient, hail_mail_base_domain)
        if domain is None:
            result.skipped_recipients.append(recipient)
            continue
        scope = domain.id if domain.kind == "custom" else domain.organization_id
        if scope in seen_scopes:
            continue
        seen_scopes.add(scope)

        # Evaluate the cap BEFORE persisting this message so the count reflects
        # how many inbound rows the org already received in the last hour.
        over_cap = await _org_over_inbound_cap(
            db, organization_id=domain.organization_id, cap=org_rate_per_hour
        )

        email_id, created = await _persist_one(
            db,
            parsed=parsed,
            message=message,
            domain=domain,
            suppress=suppress,
            s3=s3,
        )
        if email_id is None:
            continue
        result.email_ids.append(email_id)
        if created:
            result.created_email_ids.append((email_id, domain.organization_id))

        row_reasons: list[str] = []
        if over_cap:
            row_reasons.append("inbound_rate_limit")

        if (
            created
            and suppress is None
            and not over_cap
            and forward_enqueue is not None
            and domain.inbound_enabled
        ):
            funded = funds_check is None or await funds_check(
                db, domain.organization_id
            )
            if funded:
                row_reasons += await _enqueue_forwards(
                    db,
                    domain=domain,
                    parsed=parsed,
                    inbound_id=email_id,
                    hops=inbound_hops,
                    hail_mail_base_domain=hail_mail_base_domain,
                    forward_max_hops=forward_max_hops,
                    forward_default_per_hour=forward_default_per_hour,
                    forward_enqueue=forward_enqueue,
                )
            elif domain.forward_to:
                # Out of credit: keep + store + (later) charge, but don't spend
                # on SES forwards. Only flag when targets were actually configured.
                row_reasons.append("insufficient_funds")

        for reason in row_reasons:
            if reason not in result.suppressed_reasons:
                result.suppressed_reasons.append(reason)

        # Stamp the forward/rate/funds suppression reasons onto the row so the
        # console can explain why a forward didn't fire. Merged with ``||`` so
        # an existing spam/virus ``suppressed`` key is preserved — a spam row
        # that is also over the inbound cap reaches here with
        # row_reasons=["inbound_rate_limit"], and the merge keeps both keys.
        if created and row_reasons:
            await db.execute(
                update(Email)
                .where(Email.id == email_id)
                .values(
                    metadata_=Email.metadata_.op("||")(
                        func.jsonb_build_object(
                            "suppressed_reasons",
                            cast(row_reasons, JSONB),
                        )
                    )
                )
            )

        if created and suppress is None and fanout is not None:
            if not over_cap:
                data = build_event_data(
                    email_id=str(email_id),
                    direction="inbound",
                    from_address=parsed.from_address,
                    to_addresses=parsed.to_addresses
                    or list(message.envelope_recipients),
                    subject=parsed.subject or "",
                    message_id=parsed.message_id,
                    in_reply_to=parsed.in_reply_to,
                    spam_verdict=message.spam_verdict,
                    virus_verdict=message.virus_verdict,
                    spf_verdict=message.spf_verdict,
                    dkim_verdict=message.dkim_verdict,
                    dmarc_verdict=message.dmarc_verdict,
                    raw_url=(
                        join_url(api_base_url, f"emails/{email_id}/raw")
                        if api_base_url
                        else None
                    ),
                    attachments=await _attachment_payload(db, email_id, api_base_url),
                    email_domain=domain.domain,
                )
                await fanout(
                    db,
                    organization_id=domain.organization_id,
                    email_domain_id=domain.id,
                    event_type="email.received",
                    event_id=email_id,
                    data=data,
                )
            for reason in row_reasons:
                await fanout(
                    db,
                    organization_id=domain.organization_id,
                    email_domain_id=domain.id,
                    event_type="email.received.suppressed",
                    event_id=email_id,
                    data={
                        "id": str(email_id),
                        "direction": "inbound",
                        "email_domain": domain.domain,
                        "from_address": parsed.from_address,
                        "to_addresses": parsed.to_addresses
                        or list(message.envelope_recipients),
                        "subject": parsed.subject or "",
                        "message_id": parsed.message_id,
                        "reason": reason,
                    },
                )

    await db.commit()
    return result


async def _attachment_payload(
    db: AsyncSession, email_id: UUID, api_base_url: str | None
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                select(EmailAttachment).where(EmailAttachment.email_id == email_id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": str(att.id),
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": att.size_bytes,
            "content_id": att.content_id,
            "url": (
                join_url(api_base_url, f"emails/{email_id}/attachments/{att.id}")
                if api_base_url
                else None
            ),
        }
        for att in rows
    ]
