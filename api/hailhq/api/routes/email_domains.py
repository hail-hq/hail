"""Routes for managing email-sending domain identities.

POST   /email-domains             — register a custom domain or mint a hail-mail address.
GET    /email-domains             — cursor-paginated list (org-scoped).
GET    /email-domains/{id}        — single domain (org-scoped).
PATCH  /email-domains/{id}        — edit the user/org prefix on a hail-mail row.
POST   /email-domains/{id}/verify — re-poll the email provider's view of the identity.
DELETE /email-domains/{id}        — delete from provider + DB (idempotent on missing).

Two flavors of row land here:

* ``kind='custom'`` — caller supplies ``domain``, we call SES
  ``CreateEmailIdentity`` and surface the three DKIM CNAMEs they need to
  publish. The row stays ``verification_status='pending'`` until the
  caller hits the verify endpoint after publishing DNS.
* ``kind='hail_mail'`` — caller may supply ``local_prefix_user`` +
  ``local_prefix_org``; otherwise the user prefix falls back to
  ``HAIL_MAIL_DEFAULT_USER_PREFIX`` (or ``HAIL_MAIL_FROM``) and the org
  prefix is derived per-org from the organization id, so two orgs can never
  share an address. The server composes ``<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>``,
  lands the row ``verified`` immediately (the parent is pre-verified at
  SES out-of-band), and never calls SES. Org admins can later rename
  through ``PATCH``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.audit import write_audit_log
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.dns_lookup import custom_dns_records, resolve_mx, ses_inbound_host
from hailhq.core.email_sender import from_address_for
from hailhq.core.hail_mail import org_prefix_from_id
from hailhq.core.models import Email, EmailDomain
from hailhq.core.providers.email import EmailProvider, SesEmailProvider
from hailhq.core.schemas import (
    LOCAL_PREFIX,
    DomainCheckResponse,
    EmailDomainCreate,
    EmailDomainListResponse,
    EmailDomainPatch,
    EmailDomainResponse,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/email-domains", tags=["email-domains"])

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
    organization_id: UUID,
) -> tuple[str, str]:
    """Pick the user/org prefixes for a hail-mail address.

    User-prefix precedence (highest wins):

    1. Explicit ``body_user`` (the POST body's ``local_prefix_user``).
    2. ``HAIL_MAIL_FROM`` env var (single-shot ``<user>+<org>@<base>``).
    3. ``HAIL_MAIL_DEFAULT_USER_PREFIX`` env var.

    Org-prefix precedence:

    1. Explicit ``body_org``.
    2. ``HAIL_MAIL_FROM`` org part — the single-tenant self-host opt-in.
    3. ``org_prefix_from_id(organization_id)`` — derived per-org so a
       multi-tenant deployment never mints one shared address. (This
       replaced ``HAIL_MAIL_DEFAULT_ORG_PREFIX``, a deploy-wide constant
       that every org collided on: the first org claimed it and the global
       unique index 409'd the rest.)

    Returns ``(user_prefix, org_prefix)``. Raises HTTPException 503 only if
    the user prefix has no source — the org prefix is always derivable.
    """
    env_user, env_org = ("", "")
    if settings.hail_mail_from:
        env_user, env_org = _parse_hail_mail_from(settings.hail_mail_from)

    user = body_user or env_user or settings.hail_mail_default_user_prefix
    org = body_org or env_org or org_prefix_from_id(organization_id)
    if not user:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "hail-mail prefixes are not configured: missing "
                "local_prefix_user (or HAIL_MAIL_FROM / "
                "HAIL_MAIL_DEFAULT_USER_PREFIX)"
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
# Sender selection — shared by POST /emails and GET /email-domains.
# --------------------------------------------------------------------------- #


async def verified_senders(
    db: AsyncSession, organization_id: UUID
) -> list[EmailDomain]:
    """Every verified identity the org can send through, oldest first.

    Ordered by ``created_at`` so a single-identity org resolves the same
    row on every retry.
    """
    stmt = (
        select(EmailDomain)
        .where(EmailDomain.organization_id == organization_id)
        .where(EmailDomain.verification_status == "verified")
        .order_by(EmailDomain.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def first_pending_custom(db: AsyncSession, organization_id: UUID) -> str | None:
    """The domain of one custom row still waiting on DKIM, if any."""
    stmt = (
        select(EmailDomain.domain)
        .where(EmailDomain.organization_id == organization_id)
        .where(EmailDomain.kind == "custom")
        .where(EmailDomain.verification_status == "pending")
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def preview_default_from(db: AsyncSession, organization_id: UUID) -> str | None:
    """The address a ``from``-less send would use right now, or ``None``.

    ``None`` means "a ``from``-less send will not go out": either the org
    owns several verified identities and must name one (``resolve_sender``
    raises 422), or it owns none that can send yet. Mirrors
    ``resolve_sender`` case for case — the two must not drift, which is
    why both read ``verified_senders`` / ``first_pending_custom``.

    Never raises: hail-mail being unconfigured is a normal deployment
    posture and must not break ``GET /email-domains``.
    """
    verified = await verified_senders(db, organization_id)
    if len(verified) != 1:
        return None if verified else await _unminted_hail_mail(db, organization_id)
    return from_address_for(verified[0])


async def _unminted_hail_mail(db: AsyncSession, organization_id: UUID) -> str | None:
    """The hail-mail address a first send would mint, if it would mint one."""
    if await first_pending_custom(db, organization_id) is not None:
        # resolve_sender 422s on this state instead of minting.
        return None
    try:
        user_prefix, org_prefix = resolve_hail_mail_prefixes(
            None, None, organization_id
        )
        return compose_hail_mail_address(user_prefix, org_prefix)
    except HTTPException:
        # 503 from either helper — the operator has not configured
        # hail-mail. Not an error for a read; there simply is no default.
        return None


# --------------------------------------------------------------------------- #
# POST /email-domains
# --------------------------------------------------------------------------- #


@router.post(
    "",
    response_model=EmailDomainResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def create_email_domain(
    body: EmailDomainCreate,
    response: Response,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> EmailDomainResponse:
    if body.kind == "hail_mail":
        user_prefix, org_prefix = resolve_hail_mail_prefixes(
            body.local_prefix_user,
            body.local_prefix_org,
            principal.organization_id,
        )
        address = compose_hail_mail_address(user_prefix, org_prefix)
        sd = EmailDomain(
            organization_id=principal.organization_id,
            kind="hail_mail",
            domain=address,
            local_prefix_user=user_prefix,
            local_prefix_org=org_prefix,
            # Parent domain is pre-verified by the operator out-of-band.
            verification_status="verified",
            dns_records=[],
            mail_from_domain=None,
            provider="ses",
            # Shared parent identity — DELETE skips SES for hail_mail rows so
            # the parent is never passed to ses:DeleteEmailIdentity.
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
                detail=f"hail-mail address {address!r} is already registered",
            ) from exc
        await db.refresh(sd)
        await write_audit_log(
            organization_id=principal.organization_id,
            api_key_id=principal.api_key_id,
            action="email_domain.create",
            resource_type="email_domain",
            resource_id=sd.id,
            payload={"kind": "hail_mail", "domain": sd.domain},
        )
        response.headers["Location"] = f"/email-domains/{sd.id}"
        return EmailDomainResponse.model_validate(sd)

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

    sd = EmailDomain(
        organization_id=principal.organization_id,
        kind="custom",
        domain=domain,
        verification_status=identity.verification_status,
        dns_records=custom_dns_records(domain, identity.dkim_records),
        mail_from_domain=identity.mail_from_domain,
        mail_from_status=identity.mail_from_status,
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
            detail=f"sender domain {domain!r} is already registered",
        ) from exc
    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="email_domain.create",
        resource_type="email_domain",
        resource_id=sd.id,
        payload={"kind": "custom", "domain": sd.domain},
    )
    response.headers["Location"] = f"/email-domains/{sd.id}"
    return EmailDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# GET /email-domains
# --------------------------------------------------------------------------- #


@router.get(
    "",
    response_model=EmailDomainListResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def list_email_domains(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> EmailDomainListResponse:
    stmt = select(EmailDomain).where(
        EmailDomain.organization_id == principal.organization_id
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        EmailDomain.created_at,
        EmailDomain.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )

    return EmailDomainListResponse(
        items=[EmailDomainResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        # Whole-org answer, not a property of this page: computed from
        # every verified row, so it stays correct while paging.
        default_from=await preview_default_from(db, principal.organization_id),
    )


# --------------------------------------------------------------------------- #
# GET /email-domains/check-domain
# --------------------------------------------------------------------------- #


@router.get(
    "/check-domain",
    response_model=DomainCheckResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def check_domain(
    domain: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> DomainCheckResponse:
    """Does this domain already receive mail? Drives apex-vs-prefix onboarding."""
    apex = domain.strip().lower().lstrip(".")
    mx = await resolve_mx(apex)
    in_use = len(mx) > 0
    return DomainCheckResponse(
        domain=apex,
        in_use=in_use,
        existing_mx=mx,
        suggested_domain=f"inbox.{apex}" if in_use else apex,
    )


# --------------------------------------------------------------------------- #
# GET /email-domains/{id}
# --------------------------------------------------------------------------- #


@router.get(
    "/{domain_id}",
    response_model=EmailDomainResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def get_email_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailDomainResponse:
    stmt = select(EmailDomain).where(
        EmailDomain.id == domain_id,
        EmailDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )
    return EmailDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# PATCH /email-domains/{id}
# --------------------------------------------------------------------------- #


@router.patch(
    "/{domain_id}",
    response_model=EmailDomainResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def patch_email_domain(
    domain_id: UUID,
    body: EmailDomainPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailDomainResponse:
    """Edit hail-mail prefixes and/or inbound action settings.

    Two modes, mutually compatible:

    * **Prefix edit** (``local_prefix_user`` / ``local_prefix_org``):
      hail_mail rows only. The managed-cloud console writes here when
      an org admin changes the visible hail-mail address.
    * **Inbound action edit** (``inbound_enabled`` / ``forward_to`` /
      ``forward_rate_per_hour``): any row kind.
    """
    stmt = select(EmailDomain).where(
        EmailDomain.id == domain_id,
        EmailDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="email domain not found",
        )

    updates: dict = {}

    # ---- prefix edits (hail_mail only) ----
    if body.local_prefix_user is not None or body.local_prefix_org is not None:
        if sd.kind != "hail_mail":
            raise unprocessable(
                "prefix edits are only allowed on kind='hail_mail' rows",
                loc=["body", "local_prefix_user"],
            )
        new_user = body.local_prefix_user or sd.local_prefix_user
        new_org = body.local_prefix_org or sd.local_prefix_org
        assert new_user is not None and new_org is not None
        new_address = compose_hail_mail_address(new_user, new_org)
        updates["local_prefix_user"] = new_user
        updates["local_prefix_org"] = new_org
        updates["domain"] = new_address

    # ---- inbound action edits (any kind) ----
    if body.inbound_enabled is not None:
        updates["inbound_enabled"] = body.inbound_enabled
    if body.forward_to is not None:
        updates["forward_to"] = body.forward_to or None
    if body.forward_rate_per_hour is not None:
        updates["forward_rate_per_hour"] = body.forward_rate_per_hour

    if not updates:
        return EmailDomainResponse.model_validate(sd)

    try:
        await db.execute(
            update(EmailDomain).where(EmailDomain.id == sd.id).values(**updates)
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if "local_prefix_user" in updates:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT,
                detail=(
                    f"hail-mail address {updates.get('domain')!r} is already registered"
                ),
            ) from exc
        raise unprocessable("patch violates a domain check constraint") from exc

    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="email_domain.patch",
        resource_type="email_domain",
        resource_id=sd.id,
        payload={"domain": sd.domain},
    )

    return EmailDomainResponse.model_validate(sd)


# --------------------------------------------------------------------------- #
# POST /email-domains/{id}/verify
# --------------------------------------------------------------------------- #


@router.post(
    "/{domain_id}/verify",
    response_model=EmailDomainResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def verify_email_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> EmailDomainResponse:
    """Re-poll the email provider for the current verification status.

    On-demand only — there is no background poller in v1. Operators /
    tenants hit this after publishing DNS to flip the row to ``verified``.
    Hail-mail rows are no-ops (they're already verified by construction).
    """
    stmt = select(EmailDomain).where(
        EmailDomain.id == domain_id,
        EmailDomain.organization_id == principal.organization_id,
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
            action="email_domain.verify",
            resource_type="email_domain",
            resource_id=sd.id,
            payload={"domain": sd.domain, "status": sd.verification_status},
        )
        return EmailDomainResponse.model_validate(sd)

    try:
        identity = await email_provider.get_identity(sd.domain)
    except LookupError as exc:
        # Identity vanished provider-side (operator deleted it out-of-band).
        # Mark the row failed so the next POST /emails fails fast.
        await db.execute(
            update(EmailDomain)
            .where(EmailDomain.id == sd.id)
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
    new_status = identity.verification_status
    values = {
        "verification_status": new_status,
        "dns_records": custom_dns_records(sd.domain, identity.dkim_records),
        "mail_from_domain": identity.mail_from_domain,
        "mail_from_status": identity.mail_from_status,
        "verified_at": verified_at,
    }
    # Receiving turns on automatically once a custom domain verifies — no
    # separate toggle. Idempotent: re-verifying a row already True keeps it True.
    if sd.kind == "custom" and new_status == "verified":
        values["inbound_enabled"] = True
    await db.execute(
        update(EmailDomain).where(EmailDomain.id == sd.id).values(**values)
    )
    await db.commit()
    await db.refresh(sd)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action="email_domain.verify",
        resource_type="email_domain",
        resource_id=sd.id,
        payload={"domain": sd.domain, "status": sd.verification_status},
    )
    # Check whether the receive MX is live.  Placed after commit so a DNS
    # error can't roll back the verified status we just persisted.
    try:
        receive_ready: bool | None = ses_inbound_host(
            settings.aws_region
        ) in await resolve_mx(sd.domain)
    except Exception:
        logger.warning(
            "receive-MX lookup failed for domain=%s; reporting receive_ready=null",
            sd.domain,
            exc_info=True,
        )
        receive_ready = None
    return EmailDomainResponse.model_validate(sd).model_copy(
        update={"receive_ready": receive_ready}
    )


# --------------------------------------------------------------------------- #
# DELETE /email-domains/{id}
# --------------------------------------------------------------------------- #


@router.delete(
    "/{domain_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def delete_email_domain(
    domain_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
) -> Response:
    stmt = select(EmailDomain).where(
        EmailDomain.id == domain_id,
        EmailDomain.organization_id == principal.organization_id,
    )
    sd = (await db.execute(stmt)).scalar_one_or_none()
    if sd is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="sender domain not found",
        )

    # Pre-check linked emails (ON DELETE RESTRICT) before touching SES, so a
    # caller with linked rows gets a clean 409 without an SES round-trip. The
    # IntegrityError catch below covers the race between this check and commit.
    linked_stmt = select(Email.id).where(Email.email_domain_id == sd.id).limit(1)
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

    # DB first, then SES — a provider failure can't strand the row pointing
    # at a vanished identity.
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
        # Custom domains are globally unique (one org per domain — see the
        # email_domains_custom_domain_global_uq index), so deleting the
        # caller's row always means no other org still uses this SES identity.
        try:
            await email_provider.delete_identity(deleted_domain)
        except Exception:
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
        action="email_domain.delete",
        resource_type="email_domain",
        resource_id=deleted_id,
        payload={"kind": deleted_kind, "domain": deleted_domain},
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


__all__ = ["get_email_provider", "router"]
