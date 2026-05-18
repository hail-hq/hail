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
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import and_, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.idempotency import IdempotencyContext, idempotency_dep
from hailhq.api.routes.sender_domains import (
    compose_hail_mail_address,
    get_email_provider,
    resolve_hail_mail_prefixes,
)
from hailhq.core.billing import has_funds
from hailhq.core.db import get_session
from hailhq.core.models import Email, SenderDomain
from hailhq.core.providers.email import EmailProvider
from hailhq.core.schemas import (
    EmailCreate,
    EmailListResponse,
    EmailResponse,
    EmailStatus,
    EmailSummary,
    decode_cursor,
    encode_cursor,
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
) -> SenderDomain:
    """Find the SenderDomain row to send through, in priority order.

    1. Explicit ``from``: look up by full address (hail-mail row's
       ``domain`` is the full address; custom row's ``domain`` is the
       parent so we match by suffix).
    2. First verified org-owned domain by ``created_at`` (deterministic
       across retries — newest-last so the "default sender" stays
       stable as orgs add more).
    3. Mint a fresh hail-mail row on the fly if ``HAIL_MAIL_BASE_DOMAIN``
       is configured.

    Raises ``HTTPException`` if nothing resolves.
    """
    if explicit_from is not None:
        local, _, dom = explicit_from.partition("@")
        if not dom:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="from address must be a valid email (local@domain.tld)",
            )

        # Hail-mail rows store the full ``<user>+<org>@<base>`` as ``domain``;
        # custom rows store just the DNS suffix. Match either in one query so
        # the lookup is O(1) regardless of how many domains the org owns.
        stmt = (
            select(SenderDomain)
            .where(SenderDomain.organization_id == organization_id)
            .where(SenderDomain.verification_status == "verified")
            .where(
                or_(
                    and_(
                        SenderDomain.kind == "hail_mail",
                        SenderDomain.domain == explicit_from,
                    ),
                    and_(SenderDomain.kind == "custom", SenderDomain.domain == dom),
                )
            )
            .limit(1)
        )
        sd = (await db.execute(stmt)).scalar_one_or_none()
        if sd is not None:
            return sd

        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"from address {explicit_from!r} is not a verified sender for "
                "this organization"
            ),
        )

    # No explicit `from` — prefer any verified domain, ordered by created_at.
    stmt = (
        select(SenderDomain)
        .where(SenderDomain.organization_id == organization_id)
        .where(SenderDomain.verification_status == "verified")
        .order_by(SenderDomain.created_at.asc())
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
        select(SenderDomain.domain)
        .where(SenderDomain.organization_id == organization_id)
        .where(SenderDomain.kind == "custom")
        .where(SenderDomain.verification_status == "pending")
        .limit(1)
    )
    pending_domain = (await db.execute(pending_stmt)).scalar_one_or_none()
    if pending_domain is not None:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"sender domain {pending_domain!r} is pending DKIM verification; "
                "publish the DNS records and call POST /sender-domains/{id}/verify, "
                "or pass an explicit verified `from` address"
            ),
        )

    # Last resort: mint a hail-mail row from the env-var defaults. Either
    # helper bubbles a 503 if base/prefixes are unconfigured — exactly the
    # right response for a tenant who hasn't registered anything yet.
    user_prefix, org_prefix = resolve_hail_mail_prefixes(None, None)
    address = compose_hail_mail_address(user_prefix, org_prefix)
    sd = SenderDomain(
        organization_id=organization_id,
        kind="hail_mail",
        domain=address,
        local_prefix_user=user_prefix,
        local_prefix_org=org_prefix,
        verification_status="verified",
        dkim_records=[],
        mail_from_domain=None,
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    db.add(sd)
    try:
        await db.flush()
    except IntegrityError:
        # Concurrent first-send race: another request just minted the same
        # row. Roll back our insert and pick up the winning one — the
        # ``(organization_id, domain)`` unique constraint guarantees there's
        # exactly one to find.
        await db.rollback()
        existing = (
            await db.execute(
                select(SenderDomain).where(
                    SenderDomain.organization_id == organization_id,
                    SenderDomain.domain == address,
                )
            )
        ).scalar_one()
        return existing
    # Populate server-defaulted timestamps so any future read of this row
    # in the same request observes the materialized values.
    await db.refresh(sd)
    return sd


def _from_address_for(sd: SenderDomain, explicit: str | None) -> str:
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
    idem: Annotated[IdempotencyContext | None, Depends(idempotency_dep)] = None,
) -> EmailResponse:
    # Idempotency replay first — never re-send.
    if idem is not None and idem.is_replay:
        cached = idem.cached_response or {}
        if idem.cached_status and idem.cached_status >= 400:
            raise HTTPException(
                status_code=idem.cached_status,
                detail=cached.get("detail", "cached failure"),
                headers={"Idempotency-Replay": "true"},
            )
        cached_id = UUID(cached["id"])
        response.headers["Idempotency-Replay"] = "true"
        response.headers["Location"] = f"/emails/{cached_id}"
        return EmailResponse.model_validate(cached)

    # Cloud-only balance gate; shared-key auth lands on the unbilled
    # "Self-hosted" org and skips it.
    if principal.api_key_id is not None:
        if not await has_funds(db, principal.organization_id):
            raise HTTPException(
                status_code=http_status.HTTP_402_PAYMENT_REQUIRED,
                detail="insufficient credits; top up at https://hail.so/console/billing",
            )

    sd = await _resolve_sender(db, principal.organization_id, body.from_)
    from_address = _from_address_for(sd, body.from_)

    email = Email(
        organization_id=principal.organization_id,
        conversation_id=body.conversation_id,
        sender_domain_id=sd.id,
        from_address=from_address,
        to_addresses=list(body.to),
        cc_addresses=list(body.cc) if body.cc else None,
        bcc_addresses=list(body.bcc) if body.bcc else None,
        reply_to=body.reply_to,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        status="queued",
        provider="ses",
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
            "subject": email.subject,
        },
    )

    # Provider send — best-effort with status reconciliation. Synchronous
    # in v1: callers get back ``sent`` or ``failed`` on the response, no
    # background polling needed for the happy path.
    try:
        result = await email_provider.send_email(
            from_address=email.from_address,
            to_addresses=email.to_addresses,
            subject=email.subject,
            body_text=email.body_text,
            body_html=email.body_html,
            cc=email.cc_addresses,
            bcc=email.bcc_addresses,
            reply_to=email.reply_to,
        )
    except Exception as exc:
        logger.warning(
            "ses send_email failed for email_id=%s",
            email.id,
            exc_info=True,
        )
        now = datetime.now(timezone.utc)
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
        # Paired with the earlier ``email.create`` row so a compliance
        # reviewer reading the trail sees both "row created" and "send
        # failed" — the create row alone would imply success.
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="email.send_failed",
            resource_type="email",
            resource_id=email.id,
            payload={"end_reason": type(exc).__name__},
        )
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
            sent_at=now,
        )
    )
    await db.commit()
    await db.refresh(email)

    response.headers["Location"] = f"/emails/{email.id}"
    email_response = EmailResponse.model_validate(email)

    if idem is not None:
        await idem.store(
            status_code=http_status.HTTP_201_CREATED,
            body=email_response.model_dump(mode="json"),
        )

    return email_response


# --------------------------------------------------------------------------- #
# GET /emails/{id}
# --------------------------------------------------------------------------- #


@router.get("/{email_id}", response_model=EmailResponse)
async def get_email(
    email_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailResponse:
    stmt = select(Email).where(
        Email.id == email_id,
        Email.organization_id == principal.organization_id,
    )
    email = (await db.execute(stmt)).scalar_one_or_none()
    if email is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="email not found",
        )
    return EmailResponse.model_validate(email)


# --------------------------------------------------------------------------- #
# GET /emails
# --------------------------------------------------------------------------- #


@router.get("", response_model=EmailListResponse)
async def list_emails(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    status: EmailStatus | None = Query(default=None),
) -> EmailListResponse:
    stmt = select(Email).where(Email.organization_id == principal.organization_id)
    if status is not None:
        stmt = stmt.where(Email.status == status)
    if cursor is not None:
        try:
            cur_ts, cur_id = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        stmt = stmt.where(tuple_(Email.created_at, Email.id) < tuple_(cur_ts, cur_id))

    stmt = stmt.order_by(Email.created_at.desc(), Email.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        rows = rows[:limit]

    return EmailListResponse(
        items=[EmailSummary.model_validate(e) for e in rows],
        next_cursor=next_cursor,
    )


__all__ = ["router"]
