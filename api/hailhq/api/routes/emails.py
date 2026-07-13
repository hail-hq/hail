"""Routes for the v1 outbound emails API.

POST /emails - send an outbound message through SES.
GET /emails/{id} - read a single email (org-scoped).
GET /emails - cursor-paginated list (org-scoped).

The from-address resolution mirrors POST /calls:

  explicit ``from`` → first verified org-owned domain →
  freshly minted hail-mail address (if HAIL_MAIL_BASE_DOMAIN is set)

Unlike the phone-number pool, the hail-mail "pool" is not shared across
orgs — every org gets its own row under the operator's pre-verified
parent domain. Auto-mint happens on demand so the first POST /emails
from a tenant who hasn't registered a domain still goes out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated, Callable, Literal
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi import status as http_status
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, or_, select, text as text_sql, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from hailhq.api.audit import write_audit_log
from hailhq.api.consent import enforce_consent, isoformat_or_none
from hailhq.core.urls import join_url
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.idempotency import (
    IdempotencyContext,
    cache_failure,
    idempotency_dep,
    replay_cached,
)
from hailhq.api.usage import write_usage_event
from hailhq.api.routes.email_domains import (
    compose_hail_mail_address,
    get_email_provider,
    resolve_hail_mail_prefixes,
)
from hailhq.api.funds import require_funds
from hailhq.api.routes.email_accounts import (
    _CORRUPT_CREDENTIALS_DETAIL,
    _gmail_api_error_to_http,
    get_gmail_client_builder,
)
from hailhq.core.compliance_gate import check_email_allowed
from hailhq.core.db import get_session
from hailhq.core.email_delivery_events import record_sent_event
from hailhq.core.email_footer import FOOTER_SENT, append_disclosure, append_footer
from hailhq.core.models import (
    Email,
    EmailAccount,
    EmailAttachment,
    EmailDomain,
    EmailEvent,
)
from hailhq.core.s3_inbound import S3InboundClient
from hailhq.core.providers.email import EmailProvider, EmailSender
from hailhq.core.providers.email.gmail import (
    GmailApiError,
    GmailAuthError,
    GmailClient,
    GmailEmailProvider,
)
from hailhq.core.unsubscribe import build_unsubscribe_url
from hailhq.core.schemas import (
    EmailAttachmentResponse,
    EmailCreate,
    EmailEventListResponse,
    EmailEventResponse,
    EmailListResponse,
    EmailResponse,
    EmailStatsBucket,
    EmailStatsCounts,
    EmailStatsRates,
    EmailStatsResponse,
    EmailStatus,
    EmailSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/emails", tags=["emails"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200
_SEND_FAILED_DETAIL = "email send failed"


# --------------------------------------------------------------------------- #
# Sender resolution.
# --------------------------------------------------------------------------- #


async def _resolve_sender(
    db: AsyncSession,
    organization_id: UUID,
    explicit_from: str | None,
) -> EmailDomain | EmailAccount:
    """Find the EmailDomain/EmailAccount row to send through, in priority order.

    1. Explicit ``from``: a connected ``email_accounts`` row matching the
       address exactly wins first (Gmail send-as-yourself); then fall back
       to the domain lookup below (hail-mail row's ``domain`` is the full
       address; custom row's ``domain`` is the parent so we match by
       suffix).
    2. First verified org-owned domain by ``created_at`` (deterministic
       across retries — newest-last so the "default sender" stays
       stable as orgs add more).
    3. Mint a fresh hail-mail row on the fly if ``HAIL_MAIL_BASE_DOMAIN``
       is configured.

    Raises ``HTTPException`` if nothing resolves.
    """
    if explicit_from is not None:
        account = (
            await db.execute(
                select(EmailAccount).where(
                    EmailAccount.organization_id == organization_id,
                    # Google stores/returns addresses lowercased; the request's
                    # `from` may arrive mixed-case. Match case-insensitively so
                    # `Alice@Gmail.com` still resolves to the connected account.
                    func.lower(EmailAccount.email_address) == explicit_from.lower(),
                )
            )
        ).scalar_one_or_none()
        if account is not None:
            if account.status != "active":
                raise HTTPException(
                    status_code=http_status.HTTP_409_CONFLICT,
                    detail=(
                        f"connected account {explicit_from!r} is "
                        f"{account.status}; reconnect via POST "
                        f"/email-accounts/{account.id}/reconnect"
                    ),
                )
            return account

        _, _, dom = explicit_from.partition("@")
        if not dom:
            raise unprocessable(
                "from address must be a valid email (local@domain.tld)",
                loc=["body", "from"],
            )

        # Hail-mail stores the full address as ``domain``; custom stores just
        # the DNS suffix — match either shape in one query.
        stmt = (
            select(EmailDomain)
            .where(EmailDomain.organization_id == organization_id)
            .where(EmailDomain.verification_status == "verified")
            .where(
                or_(
                    and_(
                        EmailDomain.kind == "hail_mail",
                        EmailDomain.domain == explicit_from,
                    ),
                    and_(EmailDomain.kind == "custom", EmailDomain.domain == dom),
                )
            )
            .limit(1)
        )
        sd = (await db.execute(stmt)).scalar_one_or_none()
        if sd is not None:
            return sd

        raise unprocessable(
            f"from address {explicit_from!r} is not a verified sender for "
            "this organization",
            loc=["body", "from"],
        )

    # No explicit `from` — prefer any verified domain, ordered by created_at.
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.organization_id == organization_id)
        .where(EmailDomain.verification_status == "verified")
        .order_by(EmailDomain.created_at.asc())
        .limit(1)
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is not None:
        return sd

    # No verified row. If the org has *any* pending custom rows, return a
    # targeted 422 pointing at the verify endpoint rather than the generic
    # "set HAIL_MAIL_BASE_DOMAIN" message that would mislead the operator
    # into thinking hail-mail config was the problem.
    pending_stmt = (
        select(EmailDomain.domain)
        .where(EmailDomain.organization_id == organization_id)
        .where(EmailDomain.kind == "custom")
        .where(EmailDomain.verification_status == "pending")
        .limit(1)
    )
    pending_domain = (await db.execute(pending_stmt)).scalar_one_or_none()
    if pending_domain is not None:
        raise unprocessable(
            f"sender domain {pending_domain!r} is pending DKIM verification; "
            "publish the DNS records and call POST /email-domains/{id}/verify, "
            "or pass an explicit verified `from` address",
            loc=["body", "from"],
        )

    # Last resort: mint a hail-mail row. The org prefix is derived per-org
    # from the organization id, so each tenant auto-mints its OWN distinct
    # address instead of colliding on one deployment-wide default. Either
    # helper still bubbles a 503 if the base domain / user prefix is
    # unconfigured — the right response for a tenant who hasn't set up mail.
    user_prefix, org_prefix = resolve_hail_mail_prefixes(None, None, organization_id)
    address = compose_hail_mail_address(user_prefix, org_prefix)
    sd = EmailDomain(
        organization_id=organization_id,
        kind="hail_mail",
        domain=address,
        local_prefix_user=user_prefix,
        local_prefix_org=org_prefix,
        verification_status="verified",
        dns_records=[],
        mail_from_domain=None,
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    db.add(sd)
    try:
        await db.flush()
    except IntegrityError:
        # Another row already holds this hail-mail prefix pair. Two cases:
        #
        # * Same org — concurrent first-send race; another request just
        #   minted the row. Benign: pick up the winner and send through it.
        # * Another org — the global hail-mail unique index (one prefix
        #   pair per deployment) blocked us. Surface an actionable 409;
        #   silently sending through env-var defaults another tenant owns
        #   would let them intercept our replies.
        #
        # Look up WITHOUT org scoping: the global index guarantees exactly
        # one hail_mail row for this prefix pair.
        await db.rollback()
        existing = (
            await db.execute(
                select(EmailDomain).where(
                    EmailDomain.kind == "hail_mail",
                    EmailDomain.local_prefix_user == user_prefix,
                    EmailDomain.local_prefix_org == org_prefix,
                )
            )
        ).scalar_one()
        if existing.organization_id != organization_id:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=(
                    f"hail-mail address {address!r} is already claimed by "
                    "another organization; register an explicit address via "
                    "POST /email-domains with distinct local_prefix_user/"
                    "local_prefix_org"
                ),
            )
        return existing
    # Populate server-defaulted timestamps so any future read of this row
    # in the same request observes the materialized values.
    await db.refresh(sd)
    return sd


def _from_address_for(sd: EmailDomain, explicit: str | None) -> str:
    """Resolve the wire ``From:`` for a send.

    Hail-mail: ``sd.domain`` is the full address.
    Custom:    ``sd.domain`` is just the DNS name; if the caller didn't
               supply an explicit local-part, default to ``noreply``.
    """
    if explicit is not None:
        return explicit
    if sd.kind == "hail_mail":
        return sd.domain
    return f"noreply@{sd.domain}"


# --------------------------------------------------------------------------- #
# Usage events
# --------------------------------------------------------------------------- #


async def _write_usage_event(
    organization_id: UUID,
    units: int,
    ref: str,
) -> None:
    """Append one ``usage_events`` row for an outbound send.

    Thin wrapper over the shared ``write_usage_event`` helper, pinning the
    channel to ``email`` and preserving the positional call sites in this
    module.
    """
    await write_usage_event(
        organization_id=organization_id, channel="email", units=units, ref=ref
    )


# --------------------------------------------------------------------------- #
# POST /emails
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=EmailResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_email(
    body: EmailCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
    gmail_builder: Annotated[
        Callable[[EmailAccount], GmailClient], Depends(get_gmail_client_builder)
    ],
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> EmailResponse:
    # Idempotency replay first — never re-send.
    if idem is not None and idem.is_replay:
        _, cached = replay_cached(idem, response, resource_prefix="/emails")
        return EmailResponse.model_validate(cached)

    # Consent attestation gate — reject before any Email row is created.
    try:
        enforce_consent(
            recipient_consent=body.recipient_consent,
            consent_source=body.consent_source,
            message_type=body.message_type,
        )
    except HTTPException as exc:
        raise await cache_failure(idem, exc) from None

    # Compliance gate — suppression list, velocity cap. Screens every
    # recipient (to/cc/bcc), not just `to`. Also before any Email row is
    # created, so a denial has no resource to clean up; the audit entry
    # below carries resource_id=None.
    all_recipients = list(body.to) + list(body.cc or []) + list(body.bcc or [])
    gate = await check_email_allowed(db, principal.organization_id, all_recipients)
    if not gate.allowed:
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="email.blocked",
            resource_type="email",
            resource_id=None,
            payload={
                "to": body.to,
                "cc": body.cc,
                "bcc": body.bcc,
                "reason": gate.reason,
                "checks": gate.checks,
            },
        )
        raise await cache_failure(
            idem,
            HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail=gate.reason,
            ),
        )

    await require_funds(db, principal, idem)

    try:
        sd = await _resolve_sender(db, principal.organization_id, body.from_)
    except HTTPException as exc:
        raise await cache_failure(idem, exc) from None
    is_account = isinstance(sd, EmailAccount)
    from_address = sd.email_address if is_account else _from_address_for(sd, body.from_)

    email = Email(
        organization_id=principal.organization_id,
        conversation_id=body.conversation_id,
        email_domain_id=None if is_account else sd.id,
        email_account_id=sd.id if is_account else None,
        in_reply_to=body.in_reply_to,
        from_address=from_address,
        to_addresses=list(body.to),
        cc_addresses=list(body.cc) if body.cc else None,
        bcc_addresses=list(body.bcc) if body.bcc else None,
        reply_to=body.reply_to,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        status="queued",
        provider="gmail" if is_account else "ses",
        metadata_=dict(body.metadata),
    )
    db.add(email)
    await db.commit()
    await db.refresh(email)

    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="email.create",
        resource_type="email",
        resource_id=email.id,
        payload={
            "from": email.from_address,
            "to": email.to_addresses,
            "cc": email.cc_addresses,
            "bcc": email.bcc_addresses,
            "subject": email.subject,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
    )

    # Provider send — best-effort with status reconciliation. Synchronous
    # in v1: callers get back ``sent`` or ``failed`` on the response, no
    # background polling needed for the happy path.
    # Branding footer + AI disclosure ride the wire message only; the stored
    # row keeps the tenant-authored body.
    wire_text, wire_html = append_footer(
        email.body_text, email.body_html, label=FOOTER_SENT
    )
    wire_text, wire_html = append_disclosure(wire_text, wire_html)
    # One-click unsubscribe (RFC 8058) — minted per-send against the primary
    # recipient. A single send can target multiple `to` addresses; the
    # header necessarily picks one (the first) since SES/RFC only support
    # one List-Unsubscribe target per message.
    unsubscribe_url = build_unsubscribe_url(
        email.to_addresses[0], principal.organization_id
    )
    try:
        # Connected-account sends go through Gmail's REST API; everyone else
        # keeps using the injected SES-backed provider. Thread resolution
        # from `in_reply_to` happens only inside GmailEmailProvider — SES has
        # no concept of a Gmail threadId. Construction stays INSIDE the try:
        # the builder decrypts the stored refresh token and can itself raise
        # (503 config error, corrupted/rotated ciphertext) — any such failure
        # must flow through the shared failed-row + idempotency bookkeeping
        # below or the queued row and in-flight idempotency key leak.
        send_provider: EmailSender = (
            GmailEmailProvider(gmail_builder(sd)) if is_account else email_provider
        )
        result = await send_provider.send_email(
            from_address=email.from_address,
            to_addresses=email.to_addresses,
            subject=email.subject,
            body_text=wire_text,
            body_html=wire_html,
            cc=email.cc_addresses,
            bcc=email.bcc_addresses,
            reply_to=email.reply_to,
            headers={
                "List-Unsubscribe": f"<{unsubscribe_url}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                **(
                    {"In-Reply-To": body.in_reply_to, "References": body.in_reply_to}
                    if body.in_reply_to
                    else {}
                ),
            },
        )
    except Exception as exc:
        logger.warning(
            "%s send_email failed for email_id=%s",
            email.provider,
            email.id,
            exc_info=True,
        )
        now = datetime.now(timezone.utc)
        gmail_auth_failure = isinstance(exc, GmailAuthError) and is_account
        if gmail_auth_failure:
            # Same db session that persists the failed Email row below —
            # one commit covers both mutations.
            sd.status = "reauth_required"
        await db.execute(
            update(Email)
            .where(Email.id == email.id)
            .values(
                status="failed",
                end_reason=type(exc).__name__,
                failed_at=now,
            )
        )
        await db.commit()
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="email.send_failed",
            resource_type="email",
            resource_id=email.id,
            payload={"end_reason": type(exc).__name__},
        )
        if gmail_auth_failure:
            detail = (
                "Google rejected the stored credentials; reconnect via "
                f"POST /email-accounts/{sd.id}/reconnect"
            )
            if idem is not None:
                await idem.store(
                    status_code=http_status.HTTP_409_CONFLICT,
                    body={"detail": detail},
                )
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=detail,
            ) from exc
        if isinstance(exc, HTTPException):
            # e.g. the builder's 503 "HAIL_PROVIDER_SECRET_KEY must be set" —
            # keep the actionable status/detail instead of a generic 502.
            if idem is not None:
                await idem.store(
                    status_code=exc.status_code,
                    body={"detail": exc.detail},
                )
            raise exc
        if isinstance(exc, GmailApiError):
            # Non-auth Gmail failure (rate limit, invalid argument, 5xx) —
            # translate to the same status the read routes use instead of a
            # blanket 502, so a 429 stays a 429 and a 400 stays a 400.
            mapped = _gmail_api_error_to_http(exc)
            if idem is not None:
                await idem.store(
                    status_code=mapped.status_code,
                    body={"detail": mapped.detail},
                )
            raise mapped from exc
        if isinstance(exc, InvalidToken):
            # Corrupted/rotated ciphertext — same actionable detail the read
            # routes give (still a 502; the row is already marked failed).
            if idem is not None:
                await idem.store(
                    status_code=http_status.HTTP_502_BAD_GATEWAY,
                    body={"detail": _CORRUPT_CREDENTIALS_DETAIL},
                )
            raise HTTPException(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                detail=_CORRUPT_CREDENTIALS_DETAIL,
            ) from exc
        if idem is not None:
            await idem.store(
                status_code=http_status.HTTP_502_BAD_GATEWAY,
                body={"detail": _SEND_FAILED_DETAIL},
            )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail=_SEND_FAILED_DETAIL,
        ) from exc

    now = datetime.now(timezone.utc)
    await db.execute(
        update(Email)
        .where(Email.id == email.id)
        .values(
            status="sent",
            provider_message_id=result.provider_message_id,
            provider_thread_id=result.provider_thread_id,
            sent_at=now,
        )
    )
    record_sent_event(
        db, email_id=email.id, organization_id=email.organization_id, occurred_at=now
    )
    await db.commit()
    await db.refresh(email)

    # Flat 1¢ per send regardless of recipient count.
    await _write_usage_event(
        organization_id=principal.organization_id,
        units=1,
        ref=f"email:{email.id}",
    )

    response.headers["Location"] = f"/emails/{email.id}"
    email_response = EmailResponse.model_validate(email)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=email_response.model_dump(mode="json"),
        )

    return email_response


# --------------------------------------------------------------------------- #
# GET /emails/{id}/events
# --------------------------------------------------------------------------- #


_DEFAULT_EVENTS_LIMIT = 100
_MAX_EVENTS_LIMIT = 1000


@router.get("/{email_id}/events", response_model=EmailEventListResponse)
async def list_email_events(
    email_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_EVENTS_LIMIT, ge=1, le=_MAX_EVENTS_LIMIT),
) -> EmailEventListResponse:
    """Chronological lifecycle events for one email (org-scoped).

    Cursor-paginated with the same forward-walk shape as ``GET /events``:
    strictly-greater on ``(occurred_at, id)``, ascending.
    """
    exists = (
        await db.execute(
            select(Email.id).where(
                Email.id == email_id,
                Email.organization_id == principal.organization_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="email not found",
        )
    rows, next_cursor = await fetch_cursor_page(
        db,
        select(EmailEvent).where(EmailEvent.email_id == email_id),
        EmailEvent.occurred_at,
        EmailEvent.id,
        cursor=cursor,
        limit=limit,
    )

    return EmailEventListResponse(
        items=[EmailEventResponse.model_validate(e) for e in rows],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# GET /emails/stats
#
# MUST be registered above GET /{email_id} — FastAPI matches routes in
# registration order, and "stats" would otherwise be swallowed by the UUID
# path param and 422 on parse.
# --------------------------------------------------------------------------- #

_STATS_MAX_RANGE_DAYS = 92
_STATS_MAX_HOURLY_DAYS = 8

# One pass over the (organization_id, occurred_at) range: the extra
# ``(kind)`` grouping set yields window-level rows (bucket_start IS NULL)
# whose distinct counts are correct across buckets — summing per-bucket
# uniques would over-count an email that opened/clicked in more than one.
_STATS_SQL = text_sql("""
    SELECT
      date_trunc(:bucket, occurred_at) AS bucket_start,
      kind,
      count(*) AS total,
      count(DISTINCT email_id) AS unique_emails,
      count(*) FILTER (WHERE payload->>'hard' = 'true') AS hard
    FROM email_events
    WHERE organization_id = :org
      AND occurred_at >= :from_ts
      AND occurred_at < :to_ts
    GROUP BY GROUPING SETS ((date_trunc(:bucket, occurred_at), kind), (kind))
    """)


def _truncate(ts: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


@router.get("/stats", response_model=EmailStatsResponse)
async def get_email_stats(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    bucket: Literal["hour", "day"] = Query(default="day"),
) -> EmailStatsResponse:
    to_ts = to or datetime.now(timezone.utc)
    from_ts = from_ or to_ts - timedelta(days=7)
    if from_ts.tzinfo is None or to_ts.tzinfo is None:
        raise unprocessable(
            "from/to must be timezone-aware ISO 8601", loc=["query", "from"]
        )
    # Normalize to UTC before any bucket math: Postgres date_trunc below runs
    # in session UTC, so Python-side truncation/stepping must match it. A
    # request expressed in another offset (spec-legal, e.g. +02:00) would
    # otherwise produce bucket keys shifted from the SQL rows, silently
    # zeroing every totals/series lookup.
    from_ts = from_ts.astimezone(timezone.utc)
    to_ts = to_ts.astimezone(timezone.utc)
    if from_ts >= to_ts:
        raise unprocessable("'from' must be before 'to'", loc=["query", "from"])
    span = to_ts - from_ts
    if span > timedelta(days=_STATS_MAX_RANGE_DAYS):
        raise unprocessable(
            f"range exceeds {_STATS_MAX_RANGE_DAYS} days", loc=["query", "to"]
        )
    if bucket == "hour" and span > timedelta(days=_STATS_MAX_HOURLY_DAYS):
        raise unprocessable(
            f"bucket=hour limited to {_STATS_MAX_HOURLY_DAYS} days",
            loc=["query", "bucket"],
        )

    rows = (
        await db.execute(
            _STATS_SQL,
            {
                "bucket": bucket,
                "org": principal.organization_id,
                "from_ts": from_ts,
                "to_ts": to_ts,
            },
        )
    ).all()

    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    start = _truncate(from_ts, bucket)
    buckets: dict[datetime, EmailStatsBucket] = {}
    cur = start
    while cur < to_ts:
        buckets[cur] = EmailStatsBucket(bucket_start=cur)
        cur += step

    totals = EmailStatsCounts()
    for bucket_start, kind, total, unique_emails, hard in rows:
        # NULL bucket_start marks the window-level grouping set → totals.
        b = totals if bucket_start is None else buckets.get(bucket_start)
        if b is None:  # bucket before the truncated start edge
            continue
        setattr(b, kind, getattr(b, kind) + total)
        if kind == "opened":
            b.unique_opened += unique_emails
        elif kind == "clicked":
            b.unique_clicked += unique_emails
        elif kind == "bounced":
            b.bounced_hard += hard

    rates = EmailStatsRates()
    if totals.sent:
        rates.delivery = totals.delivered / totals.sent
        rates.bounce = totals.bounced_hard / totals.sent
        rates.complaint = totals.complained / totals.sent
        rates.open = totals.unique_opened / totals.sent
        rates.click = totals.unique_clicked / totals.sent

    return EmailStatsResponse(
        from_ts=from_ts,
        to_ts=to_ts,
        bucket=bucket,
        totals=totals,
        rates=rates,
        series=list(buckets.values()),  # inserted in ascending bucket order
    )


# --------------------------------------------------------------------------- #
# GET /emails/{id}
# --------------------------------------------------------------------------- #


@router.get("/{email_id}", response_model=EmailResponse)
async def get_email(
    email_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailResponse:
    last_event_at = (
        select(func.max(EmailEvent.occurred_at))
        .where(EmailEvent.email_id == Email.id)
        .scalar_subquery()
    )
    stmt = select(Email, last_event_at).where(
        Email.id == email_id,
        Email.organization_id == principal.organization_id,
    )
    row = (await db.execute(stmt)).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="email not found",
        )
    email, last_event = row
    resp = EmailResponse.model_validate(email)
    resp.last_event_at = last_event
    base = str(request.base_url)
    if email.raw_s3_key:
        resp.raw_url = join_url(base, f"emails/{email.id}/raw")
    att_rows = (
        (
            await db.execute(
                select(EmailAttachment).where(EmailAttachment.email_id == email.id)
            )
        )
        .scalars()
        .all()
    )
    resp.attachments = [
        EmailAttachmentResponse(
            id=a.id,
            filename=a.filename,
            content_type=a.content_type,
            size_bytes=a.size_bytes,
            content_id=a.content_id,
            url=join_url(base, f"emails/{email.id}/attachments/{a.id}"),
        )
        for a in att_rows
    ]
    return resp


# --------------------------------------------------------------------------- #
# GET /emails
# --------------------------------------------------------------------------- #


def _get_s3_inbound() -> S3InboundClient:
    from hailhq.core.config import settings as _s

    return S3InboundClient(bucket=_s.hail_inbound_bucket)


@router.get("/{email_id}/raw")
async def get_email_raw(
    email_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3InboundClient, Depends(_get_s3_inbound)],
) -> Response:
    """302 → presigned S3 URL for the raw inbound MIME (404 for outbound)."""
    stmt = select(Email).where(
        Email.id == email_id,
        Email.organization_id == principal.organization_id,
    )
    email = (await db.execute(stmt)).scalar_one_or_none()
    if email is None or email.direction != "inbound" or not email.raw_s3_key:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="raw MIME not available",
        )
    url = await s3.presign_get(email.raw_s3_key, ttl_seconds=300)
    return RedirectResponse(url=url, status_code=http_status.HTTP_302_FOUND)


@router.get("/{email_id}/attachments/{attachment_id}")
async def get_email_attachment(
    email_id: UUID,
    attachment_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3InboundClient, Depends(_get_s3_inbound)],
) -> Response:
    """302 → presigned S3 URL for one attachment."""
    stmt = (
        select(EmailAttachment)
        .join(Email, Email.id == EmailAttachment.email_id)
        .where(EmailAttachment.id == attachment_id)
        .where(Email.id == email_id)
        .where(Email.organization_id == principal.organization_id)
    )
    att = (await db.execute(stmt)).scalar_one_or_none()
    if att is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="attachment not found",
        )
    url = await s3.presign_get(att.s3_key, ttl_seconds=300)
    return RedirectResponse(url=url, status_code=http_status.HTTP_302_FOUND)


@router.get("", response_model=EmailListResponse)
async def list_emails(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: EmailStatus | None = Query(default=None),
    direction: Literal["outbound", "inbound"] | None = Query(default=None),
) -> EmailListResponse:
    stmt = (
        select(Email)
        .options(defer(Email.body_text), defer(Email.body_html))
        .where(Email.organization_id == principal.organization_id)
    )
    if status is not None:
        stmt = stmt.where(Email.status == status)
    if direction is not None:
        stmt = stmt.where(Email.direction == direction)
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        Email.created_at,
        Email.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )

    return EmailListResponse(
        items=[EmailSummary.model_validate(e) for e in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
