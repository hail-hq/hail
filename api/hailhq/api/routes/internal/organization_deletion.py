"""Fail-closed external cleanup for website-owned organization deletion.

The website locks the owner membership and organization row, calls this route,
then purges scoped rows and the organization in one transaction. Reuse
org_closures for the deletion tombstone: it holds lifecycle state already.
No new table fits better. Its tombstone and retired mail addresses survive.
No Stripe calls belong here. References remain until the website commits, so
failed provider/storage cleanup can be retried without losing its inputs.
"""

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from hailhq.api.number_orders import retry_release
from hailhq.api.routes.calls import get_livekit_optional
from hailhq.api.routes.email_domains import get_email_provider
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.numbers import release_org_number
from hailhq.api.routes.verifications import (
    Registry,
    _detach_registration,
    _rate_before_wipe,
    get_verification_registry,
)
from hailhq.core import inbound_routing
from hailhq.core.carrier_routing import CARRIERS, sms_route
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import (
    Call,
    CarrierVerification,
    Email,
    EmailAttachment,
    EmailAttachmentUpload,
    EmailDomain,
    Organization,
    OrganizationMember,
    OrgClosure,
    PhoneNumber,
    Sms,
)
from hailhq.core.providers.email import EmailProvider
from hailhq.core.recordings import delete_recording
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/internal/organization-deletion",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class OrganizationDeletionIn(BaseModel):
    organization_id: UUID
    user_id: UUID
    confirmation_name: str


def optional_mail_storage() -> S3MailClient | None:
    return (
        S3MailClient(bucket=settings.hail_mail_bucket)
        if settings.hail_mail_bucket
        else None
    )


async def check_pending(db: AsyncSession, org: UUID) -> None:
    checks = (
        (PhoneNumber, PhoneNumber.provisioning_state == "pending"),
        (
            CarrierVerification,
            CarrierVerification.state.in_(("submitting", "submitted")),
        ),
        (Call, Call.status.not_in(TERMINAL_CALL_STATUSES)),
        (
            Sms,
            or_(
                Sms.status == "queued",
                Sms.agent_reply_state.in_(("pending", "processing")),
            ),
        ),
        (Email, Email.status == "queued"),
    )
    for model, condition in checks:
        pending = (
            await db.execute(
                select(model.id).where(model.organization_id == org, condition).limit(1)
            )
        ).first()
        if pending:
            raise HTTPException(
                409,
                "Finish pending calls, messages, number orders, and verifications before deleting.",
            )


@router.post("/prepare")
async def prepare_organization_deletion(
    body: OrganizationDeletionIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    email_provider: Annotated[EmailProvider, Depends(get_email_provider)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
    registry: Annotated[Registry, Depends(get_verification_registry)],
    mail: Annotated[S3MailClient | None, Depends(optional_mail_storage)],
) -> dict:
    org = body.organization_id
    owner = (
        await db.execute(
            select(Organization.name, OrganizationMember.role)
            .join(
                OrganizationMember,
                OrganizationMember.organization_id == Organization.id,
            )
            .where(Organization.id == org, OrganizationMember.user_id == body.user_id)
        )
    ).first()
    if owner is None or owner.role != "owner":
        raise HTTPException(
            403, "Only an organization owner can delete this organization."
        )
    if body.confirmation_name != owner.name:
        raise HTTPException(
            400, "Type the exact organization name to confirm deletion."
        )
    await check_pending(db, org)
    # Persist before irreversible cleanup. A retry remains possible through
    # this HMAC-only route; ordinary API requests can no longer start work.
    await db.execute(
        insert(OrgClosure)
        .values(
            organization_id=org,
            closed_at=datetime.now(timezone.utc),
            source="organization_delete",
        )
        .on_conflict_do_update(
            index_elements=["organization_id"], set_={"source": "organization_delete"}
        )
    )
    await db.commit()
    await check_pending(db, org)

    numbers = (
        (
            await db.execute(
                select(PhoneNumber).where(PhoneNumber.organization_id == org)
            )
        )
        .scalars()
        .all()
    )
    for number in numbers:
        if (number.provisioning_metadata or {}).get("unreleased_resource_id"):
            await retry_release(db, number)
        if number.inbound_registered_at is not None:
            if lk is None:
                raise HTTPException(
                    503,
                    "Phone routing cleanup is unavailable. Contact support, then retry deletion.",
                )
            await inbound_routing.unregister(db, lk, number)
        await release_org_number(db, number, lk)

    verifications = (
        (
            await db.execute(
                select(CarrierVerification).where(
                    CarrierVerification.organization_id == org
                )
            )
        )
        .scalars()
        .all()
    )
    service_ids: dict[str, set[str]] = {}
    for number in numbers:
        carrier = CARRIERS.get(number.provider)
        if carrier is not None and carrier.sms_route is not None:
            # A failed first attach may leave a named container without
            # ever saving its ID on the number. Scan this carrier too.
            service_ids.setdefault(number.provider, set())
        if number.messaging_service_sid:
            service_ids.setdefault(number.provider, set()).add(
                number.messaging_service_sid
            )
    for row in verifications:
        if row.purpose == "sms":
            ids = service_ids.setdefault(row.provider, set())
            for key in ("messaging_service_sid", "messaging_profile_id"):
                if row.provider_refs.get(key):
                    ids.add(row.provider_refs[key])
    for row in verifications:
        provider = registry(row.provider, row.purpose)
        if provider is None and (row.provider_refs or row.state == "approved"):
            raise HTTPException(
                503,
                "Registration cleanup is unavailable. Contact support, then retry deletion.",
            )
        if row.state == "approved" and row.purpose == "sms":
            await _rate_before_wipe(db, row, provider)
            await _detach_registration(db, row, provider)
        if provider is not None:
            # Full erasure also discards confirmed brands intentionally kept
            # by the ordinary rejected-registration dismissal flow.
            await provider.discard(row.provider_refs, strict=True)
            row.state = "cancelled"
            # Retain retry IDs until the website's final purge commits. A
            # later cleanup step can fail after this cancellation succeeds.
            row.cancelled_at = datetime.now(timezone.utc)
            await db.commit()

    # Several filings can reuse one identity. The creator may run before
    # its last reused address is removed, so finish identity cleanup only
    # after every filing's first pass. Shared identities stay at the carrier.
    for row in verifications:
        if row.provider_refs.get("identity_created") and row.provider_refs.get(
            "identity_id"
        ):
            provider = registry(row.provider, row.purpose)
            if provider is not None:
                await provider.discard(row.provider_refs, strict=True)

    used_sms_providers = (
        (
            await db.execute(
                select(Sms.provider).where(Sms.organization_id == org).distinct()
            )
        )
        .scalars()
        .all()
    )
    for provider_name in used_sms_providers:
        service_ids.setdefault(provider_name, set())
    for provider_name, ids in service_ids.items():
        await sms_route(provider_name).delete_organization_services(org, ids)

    calls = (
        (await db.execute(select(Call).where(Call.organization_id == org)))
        .scalars()
        .all()
    )
    for call in calls:
        # A terminal call's egress may still be writing audio. Keep its row
        # until that process ends, rather than lose the erasure tombstone.
        meta = call.metadata_ or {}
        if meta.get("recording_object_key") and meta.get("recording_status") not in {
            "ready",
            "failed",
            "erased",
            "disabled",
        }:
            raise HTTPException(
                409, "A recording is still processing. Wait, then retry deletion."
            )
        await delete_recording(call)
    await db.commit()

    raw = (
        (
            await db.execute(
                select(Email.raw_s3_key).where(
                    Email.organization_id == org, Email.raw_s3_key.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    attachments = (
        (
            await db.execute(
                select(EmailAttachment.s3_key)
                .join(Email, Email.id == EmailAttachment.email_id)
                .where(Email.organization_id == org)
            )
        )
        .scalars()
        .all()
    )
    uploads = (
        (
            await db.execute(
                select(EmailAttachmentUpload.s3_key).where(
                    EmailAttachmentUpload.organization_id == org
                )
            )
        )
        .scalars()
        .all()
    )
    keys = set(raw) | set(attachments) | set(uploads)
    if keys:
        # One inbound MIME object can back copies delivered to several orgs.
        # Attachments/uploads can also reference a reused storage key.
        shared_raw = (
            (
                await db.execute(
                    select(Email.raw_s3_key).where(
                        Email.organization_id != org, Email.raw_s3_key.in_(keys)
                    )
                )
            )
            .scalars()
            .all()
        )
        shared_attachments = (
            (
                await db.execute(
                    select(EmailAttachment.s3_key)
                    .join(Email, Email.id == EmailAttachment.email_id)
                    .where(
                        Email.organization_id != org, EmailAttachment.s3_key.in_(keys)
                    )
                )
            )
            .scalars()
            .all()
        )
        shared_uploads = (
            (
                await db.execute(
                    select(EmailAttachmentUpload.s3_key).where(
                        EmailAttachmentUpload.organization_id != org,
                        EmailAttachmentUpload.s3_key.in_(keys),
                    )
                )
            )
            .scalars()
            .all()
        )
        keys -= set(shared_raw) | set(shared_attachments) | set(shared_uploads)
    if keys and mail is None:
        raise HTTPException(
            503, "File cleanup is unavailable. Contact support, then retry deletion."
        )
    for key in keys:
        await mail.delete(key)

    domains = (
        (
            await db.execute(
                select(EmailDomain).where(
                    EmailDomain.organization_id == org, EmailDomain.kind == "custom"
                )
            )
        )
        .scalars()
        .all()
    )
    for domain in domains:
        # Call the provider directly: retain the row until final SQL deletion
        # and propagate errors instead of the normal route's best-effort log.
        await email_provider.delete_identity(domain.domain)
    await check_pending(db, org)
    return {"organization_id": str(org), "ready": True}
