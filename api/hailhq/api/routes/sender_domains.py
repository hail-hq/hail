"""Routes for managing email-sending domain identities.

POST   /sender-domains             — register a custom domain or mint a hail-mail address.
GET    /sender-domains             — cursor-paginated list (org-scoped).
GET    /sender-domains/{id}        — single domain (org-scoped).
PATCH  /sender-domains/{id}        — edit the user/org prefix on a hail-mail row.
POST   /sender-domains/{id}/verify — re-poll the email provider's view of the identity.
DELETE /sender-domains/{id}        — delete from provider + DB (idempotent on missing).

Two flavors of row land here:

* ``kind='custom'`` — caller supplies ``domain``, we call SES
  ``CreateEmailIdentity`` and surface the three DKIM CNAMEs they need to
  publish. The row stays ``verification_status='pending'`` until the
  caller hits the verify endpoint after publishing DNS.
* ``kind='hail_mail'`` — caller supplies ``local_prefix_user`` +
  ``local_prefix_org`` (or relies on the ``HAIL_MAIL_DEFAULT_*_PREFIX``
  env vars). The server composes ``<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>``,
  lands the row ``verified`` immediately (the parent is pre-verified at
  SES out-of-band), and never calls SES. Org admins can later rename
  through ``PATCH``; on self-hosted Hail the env vars are the only knob.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import Email, SenderDomain
from hailhq.core.providers.email import EmailProvider, SesEmailProvider
from hailhq.core.schemas import (
    LOCAL_PREFIX,
    SenderDomainCreate,
    SenderDomainListResponse,
    SenderDomainPatch,
    SenderDomainResponse,
    decode_cursor,
    encode_cursor,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sender-domains", tags=["sender-domains"])

_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 200


# --------------------------------------------------------------------------- #
# Email-provider dependency (overridable in tests).
# --------------------------------------------------------------------------- #


_email_provider_singleton: EmailProvider | None = None


async def get_email_provider() -> EmailProvider:
    """Process-wide ``EmailProvider`` built lazily on first use.

    Lazy so the AWS SDK isn't imported / a SESv2 client isn't built until
    a route needs it — keeps import-time settings out of the hot path and
    lets the rest of the API boot with no AWS creds at all.
    """
    global _email_provider_singleton
    if _email_provider_singleton is None:
        _email_provider_singleton = SesEmailProvider()
    return _email_provider_singleton


def _parse_hail_mail_from(addr: str) -> tuple[str, str]:
    """Split ``HAIL_MAIL_FROM`` into ``(user_prefix, org_prefix)``.

    Raises HTTPException 503 with a specific message on malformed input
    — wrong shape, prefix charset violations, or domain mismatch against
    ``HAIL_MAIL_BASE_DOMAIN``. The misconfiguration belongs to the
    operator's ``.env``, so 503 (service-unavailable) is the right shape:
    the API is up but the configured From-address is broken.
    """
    local, _, domain = addr.partition("@")
    if not local or not domain:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"HAIL_MAIL_FROM={addr!r} is not a valid email "
                "(expected <user>+<org>@<HAIL_MAIL_BASE_DOMAIN>)"
            ),
        )
    base = settings.hail_mail_base_domain
    if base and domain != base:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"HAIL_MAIL_FROM domain {domain!r} must match "
                f"HAIL_MAIL_BASE_DOMAIN ({base!r})"
            ),
        )
    user, sep, org = local.partition("+")
    if not sep or not user or not org:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"HAIL_MAIL_FROM local-part {local!r} must be "
                "<user>+<org> (e.g. admin+selfhost)"
            ),
        )
    if not LOCAL_PREFIX.match(user) or not LOCAL_PREFIX.match(org):
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"HAIL_MAIL_FROM prefixes ({user!r}, {org!r}) must each match "
                "^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$"
            ),
        )
    return user, org


def resolve_hail_mail_prefixes(
    body_user: str | None,
    body_org: str | None,
) -> tuple[str, str]:
    """Pick prefixes from the body or fall back to settings defaults.

    Precedence (highest wins):

    1. Explicit ``body_user`` / ``body_org`` arguments (the POST body's
       ``local_prefix_user`` / ``local_prefix_org`` fields).
    2. ``HAIL_MAIL_FROM`` env var (single-shot ``<user>+<org>@<base>``).
    3. ``HAIL_MAIL_DEFAULT_USER_PREFIX`` / ``HAIL_MAIL_DEFAULT_ORG_PREFIX``
       env vars (split form, useful for managed-cloud deploys that want
       per-prefix control without a fixed From address).

    Returns ``(user_prefix, org_prefix)``. Raises HTTPException 503 if a
    prefix is required but no source supplied one.
    """
    env_user, env_org = ("", "")
    if settings.hail_mail_from:
        env_user, env_org = _parse_hail_mail_from(settings.hail_mail_from)

    user = body_user or env_user or settings.hail_mail_default_user_prefix
    org = body_org or env_org or settings.hail_mail_default_org_prefix
    missing: list[str] = []
    if not user:
        missing.append(
            "local_prefix_user (or HAIL_MAIL_FROM / HAIL_MAIL_DEFAULT_USER_PREFIX)"
        )
    if not org:
        missing.append(
            "local_prefix_org (or HAIL_MAIL_FROM / HAIL_MAIL_DEFAULT_ORG_PREFIX)"
        )
    if missing:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "hail-mail prefixes are not configured: missing " + ", ".join(missing)
            ),
        )
    return user, org


def compose_hail_mail_address(user_prefix: str, org_prefix: str) -> str:
    """``<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>`` — fails fast if base is unset."""
    base = settings.hail_mail_base_domain
    if not base:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "hail-mail is not configured on this server; set "
                "HAIL_MAIL_BASE_DOMAIN to enable, or register a custom domain "
                "with kind='custom'"
            ),
        )
    return f"{user_prefix}+{org_prefix}@{base}"


# --------------------------------------------------------------------------- #
# POST /sender-domains
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=SenderDomainResponse,
    status_code=http_status.HTTP_201_CREATED,
)
async def create_sender_domain(
    body: SenderDomainCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> SenderDomainResponse:
    if body.kind == "hail_mail":
        user_prefix, org_prefix = resolve_hail_mail_prefixes(
            body.local_prefix_user, body.local_prefix_org
        )
        address = compose_hail_mail_address(user_prefix, org_prefix)
        sd = SenderDomain(
            organization_id=principal.organization_id,
            kind="hail_mail",
            domain=address,
            local_prefix_user=user_prefix,
            local_prefix_org=org_prefix,
            # Parent domain is pre-verified by the operator out-of-band.
            verification_status="verified",
            dkim_records=[],
            mail_from_domain=None,
            provider="ses",
            provider_resource_id=settings.hail_mail_base_domain or None,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(sd)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=(
                    f"hail-mail address {address!r} is already registered "
                    "for this organization"
                ),
            ) from exc
        await db.refresh(sd)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sender_domain.create",
            resource_type="sender_domain",
            resource_id=sd.id,
            payload={"kind": "hail_mail", "domain": sd.domain},
        )
        response.headers["Location"] = f"/sender-domains/{sd.id}"
        return SenderDomainResponse.model_validate(sd)

    # kind == 'custom' — call SES, persist DKIM records, ask the tenant to
    # publish DNS. The row stays pending until POST /verify flips it.
    assert body.domain is not None  # validator enforced
    domain = body.domain

    try:
        identity = await email_provider.create_identity(domain)
    except Exception as exc:
        logger.warning(
            "ses create_identity failed for org=%s domain=%s",
            principal.organization_id,
            domain,
            exc_info=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="email provider rejected the domain; check provider logs",
        ) from exc

    sd = SenderDomain(
        organization_id=principal.organization_id,
        kind="custom",
        domain=domain,
        verification_status=identity.verification_status,
        dkim_records=[r.model_dump() for r in identity.dkim_records],
        mail_from_domain=identity.mail_from_domain,
        provider="ses",
        provider_resource_id=identity.provider_resource_id,
    )
    db.add(sd)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=f"sender domain {domain!r} is already registered for this organization",
        ) from exc
    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sender_domain.create",
        resource_type="sender_domain",
        resource_id=sd.id,
        payload={"kind": "custom", "domain": sd.domain},
    )
    response.headers["Location"] = f"/sender-domains/{sd.id}"
    return SenderDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# GET /sender-domains
# --------------------------------------------------------------------------- #


@router.get("", response_model=SenderDomainListResponse)
async def list_sender_domains(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> SenderDomainListResponse:
    stmt = select(SenderDomain).where(
        SenderDomain.organization_id == principal.organization_id
    )
    if cursor is not None:
        try:
            cur_ts, cur_id = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        stmt = stmt.where(
            tuple_(SenderDomain.created_at, SenderDomain.id) < tuple_(cur_ts, cur_id)
        )

    stmt = stmt.order_by(SenderDomain.created_at.desc(), SenderDomain.id.desc()).limit(
        limit + 1
    )
    rows = list((await db.execute(stmt)).scalars().all())

    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        rows = rows[:limit]

    return SenderDomainListResponse(
        items=[SenderDomainResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


# --------------------------------------------------------------------------- #
# GET /sender-domains/{id}
# --------------------------------------------------------------------------- #


@router.get("/{domain_id}", response_model=SenderDomainResponse)
async def get_sender_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderDomainResponse:
    stmt = select(SenderDomain).where(
        SenderDomain.id == domain_id,
        SenderDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )
    return SenderDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# PATCH /sender-domains/{id}
# --------------------------------------------------------------------------- #


@router.patch("/{domain_id}", response_model=SenderDomainResponse)
async def patch_sender_domain(
    domain_id: UUID,
    body: SenderDomainPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SenderDomainResponse:
    """Edit the user/org prefix on a hail-mail row.

    The managed-cloud console writes here when an org admin changes the
    visible hail-mail address. Self-hosted operators usually skip this
    endpoint and set the env-var defaults instead, but the route works
    the same way on both surfaces.

    Only ``kind='hail_mail'`` rows accept edits — there's nothing in a
    custom-domain row that the tenant should be free to mutate via this
    surface (DNS, DKIM, verification state are provider-driven).
    """
    stmt = select(SenderDomain).where(
        SenderDomain.id == domain_id,
        SenderDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )

    if sd.kind != "hail_mail":
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="prefix edits are only allowed on kind='hail_mail' rows",
        )

    new_user = body.local_prefix_user or sd.local_prefix_user
    new_org = body.local_prefix_org or sd.local_prefix_org
    assert new_user is not None and new_org is not None  # CHECK guarantees it
    new_address = compose_hail_mail_address(new_user, new_org)

    try:
        await db.execute(
            update(SenderDomain)
            .where(SenderDomain.id == sd.id)
            .values(
                local_prefix_user=new_user,
                local_prefix_org=new_org,
                domain=new_address,
            )
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"hail-mail address {new_address!r} is already registered "
                "for this organization"
            ),
        ) from exc

    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sender_domain.patch",
        resource_type="sender_domain",
        resource_id=sd.id,
        payload={"domain": sd.domain},
    )
    return SenderDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# POST /sender-domains/{id}/verify
# --------------------------------------------------------------------------- #


@router.post("/{domain_id}/verify", response_model=SenderDomainResponse)
async def verify_sender_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> SenderDomainResponse:
    """Re-poll the email provider for the current verification status.

    On-demand only — there is no background poller in v1. Operators /
    tenants hit this after publishing DNS to flip the row to ``verified``.
    Hail-mail rows are no-ops (they're already verified by construction).
    """
    stmt = select(SenderDomain).where(
        SenderDomain.id == domain_id,
        SenderDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )

    if sd.kind == "hail_mail":
        # Nothing to refresh — the parent identity is pre-verified. Still
        # audit-log the call so org admins can see who touched what.
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="sender_domain.verify",
            resource_type="sender_domain",
            resource_id=sd.id,
            payload={"domain": sd.domain, "status": sd.verification_status},
        )
        return SenderDomainResponse.model_validate(sd)

    try:
        identity = await email_provider.get_identity(sd.domain)
    except LookupError as exc:
        # Identity vanished provider-side (operator deleted it out-of-band).
        # Mark the row failed so the next POST /emails fails fast.
        await db.execute(
            update(SenderDomain)
            .where(SenderDomain.id == sd.id)
            .values(verification_status="failed")
        )
        await db.commit()
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="domain identity no longer exists with the email provider",
        ) from exc
    except Exception as exc:
        logger.warning(
            "ses get_identity failed for org=%s domain=%s",
            principal.organization_id,
            sd.domain,
            exc_info=True,
        )
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="email provider unavailable; try again shortly",
        ) from exc

    verified_at = (
        datetime.now(timezone.utc)
        if identity.verification_status == "verified" and sd.verified_at is None
        else sd.verified_at
    )
    await db.execute(
        update(SenderDomain)
        .where(SenderDomain.id == sd.id)
        .values(
            verification_status=identity.verification_status,
            dkim_records=[r.model_dump() for r in identity.dkim_records],
            mail_from_domain=identity.mail_from_domain,
            verified_at=verified_at,
        )
    )
    await db.commit()
    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sender_domain.verify",
        resource_type="sender_domain",
        resource_id=sd.id,
        payload={"domain": sd.domain, "status": sd.verification_status},
    )
    return SenderDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# DELETE /sender-domains/{id}
# --------------------------------------------------------------------------- #


@router.delete("/{domain_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_sender_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> Response:
    stmt = select(SenderDomain).where(
        SenderDomain.id == domain_id,
        SenderDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )

    # ``emails.sender_domain_id`` is ``ON DELETE RESTRICT`` — Postgres would
    # raise IntegrityError on the commit below. Surface a clean 409 instead
    # of a 500, and check before touching the email provider so a partial
    # failure can't strand the SES identity ahead of a DB row that won't go.
    linked_stmt = select(Email.id).where(Email.sender_domain_id == sd.id).limit(1)
    if (await db.execute(linked_stmt)).scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"sender domain {sd.domain!r} has linked emails; delete those "
                "rows first (or wait for retention) before deleting the sender"
            ),
        )

    # Snapshot id+domain BEFORE the delete; SQLAlchemy may expire the
    # attributes on `sd` after `await db.delete(sd)` completes.
    deleted_id = sd.id
    deleted_kind = sd.kind
    deleted_domain = sd.domain

    # Order: DB delete first, then SES. If a concurrent send sneaks in an
    # email row between the pre-check above and the commit below, the FK
    # raises IntegrityError and we still 409 — SES untouched. SES delete
    # only runs after the DB row is gone, so a provider failure can't leave
    # an orphaned row pointing at a vanished identity.
    await db.delete(sd)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"sender domain {deleted_domain!r} has linked emails; delete "
                "those rows first before deleting the sender"
            ),
        ) from exc

    if deleted_kind == "custom":
        try:
            await email_provider.delete_identity(deleted_domain)
        except Exception:
            # DB row already gone; an orphaned SES identity is benign
            # (operator can prune it manually). Log loudly so it shows up
            # in the operator's error feed.
            logger.warning(
                "ses delete_identity failed after DB delete for org=%s domain=%s "
                "(SES identity may be orphaned)",
                principal.organization_id,
                deleted_domain,
                exc_info=True,
            )

    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="sender_domain.delete",
        resource_type="sender_domain",
        resource_id=deleted_id,
        payload={"kind": deleted_kind, "domain": deleted_domain},
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["router", "get_email_provider"]
