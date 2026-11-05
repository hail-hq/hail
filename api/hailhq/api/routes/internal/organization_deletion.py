"""Fail-closed external cleanup for website-owned organization deletion.

The website serializes account/org teardown, locks the owner membership, and
calls this route. This route stops admission, drains existing work, then
publishes a final marker before cleanup or rejection of metadata writes.
The website then locks the organization and purges scoped rows atomically. Reuse
org_closures for the deletion tombstone: it holds lifecycle state already.
No new table fits better. Its tombstone and retired mail addresses survive.
No Stripe calls belong here. References remain until the website commits, so
failed provider/storage cleanup can be retried without losing its inputs.
"""

import asyncio
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
from hailhq.core.db import (
    ORGANIZATION_DELETE,
    ORGANIZATION_DELETE_PREPARING,
    get_session,
)
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
from hailhq.core.recordings import EGRESS_TERMINAL, delete_recording
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from livekit import api
from pydantic import BaseModel
from sqlalchemy import Text, any_, bindparam, delete, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY, insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/internal/organization-deletion",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)

_DRAIN_TIMEOUT_SECONDS = 5.0
_DRAIN_RETRY_SECONDS = 0.05


async def _drain_organization(db: AsyncSession, org: UUID) -> None:
    # Never queue FOR UPDATE behind an admission guard: the admitted route
    # may still need KEY SHARE on another connection to save provider IDs.
    deadline = asyncio.get_running_loop().time() + _DRAIN_TIMEOUT_SECONDS
    while True:
        try:
            await db.execute(
                select(Organization.id)
                .where(Organization.id == org)
                .with_for_update(nowait=True)
            )
            return
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            await db.rollback()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise HTTPException(
                409, "Work is still finishing. Wait, then retry deletion."
            )
        await asyncio.sleep(min(_DRAIN_RETRY_SECONDS, remaining))


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


async def ensure_recording_stopped(call: Call, lk: LiveKitClient | None) -> None:
    meta = call.metadata_ or {}
    if not meta.get("recording_object_key") or meta.get("recording_status") in {
        "ready",
        "failed",
    }:
        return
    if lk is None or not call.livekit_room:
        raise HTTPException(
            503,
            "Recording cleanup is unavailable. Contact support, then retry deletion.",
        )
    # Closing a terminal call's room prevents an ambiguous start from
    # beginning a new upload after the reconciliation query below.
    try:
        await asyncio.wait_for(lk.delete_room(call.livekit_room), timeout=5)
    except api.ServerError as exc:
        if exc.code != api.ServerErrorCode.NOT_FOUND:
            raise
    egress_id = meta.get("recording_egress_id")
    infos = await asyncio.wait_for(
        (
            lk.list_egress(egress_id)
            if egress_id
            else lk.list_room_egress(call.livekit_room)
        ),
        timeout=5,
    )
    if any(info.status not in EGRESS_TERMINAL for info in infos):
        # An erasure tombstone must outlive the final Egress upload.
        raise HTTPException(
            409, "A recording is still processing. Wait, then retry deletion."
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
    # Stop new API work while already-admitted requests finish saving their
    # provider/storage references. The strict DB trigger rejects only the
    # final marker, published after those writers drain.
    marker = await db.execute(
        insert(OrgClosure)
        .values(
            organization_id=org,
            closed_at=datetime.now(timezone.utc),
            source=ORGANIZATION_DELETE_PREPARING,
        )
        .on_conflict_do_nothing(index_elements=["organization_id"])
        .returning(OrgClosure.organization_id)
    )
    created_marker = marker.scalar_one_or_none() is not None
    if not created_marker:
        await db.execute(
            update(OrgClosure)
            .where(
                OrgClosure.organization_id == org,
                OrgClosure.source != ORGANIZATION_DELETE,
            )
            .values(source=ORGANIZATION_DELETE_PREPARING)
        )
    await db.commit()
    # Closing is visible before taking the row lock. Existing billing writers
    # drain without a row/advisory-lock cycle; new debits skip the closed org.
    try:
        await _drain_organization(db, org)
        closure = await db.get(OrgClosure, org, populate_existing=True)
        if closure is None:
            raise HTTPException(
                409, "Deletion state changed. Reload settings and retry."
            )
        await check_pending(db, org)
    except HTTPException:
        if created_marker:
            # Nothing irreversible has run. Restore access so a request that
            # raced admission can finish its review or other pending work.
            # The website serializes attempts. Delete only our preparing
            # marker; an existing final marker must never reopen access.
            await db.execute(
                delete(OrgClosure).where(
                    OrgClosure.organization_id == org,
                    OrgClosure.source == ORGANIZATION_DELETE_PREPARING,
                )
            )
            await db.commit()
        raise

    # Publish final closure while the drained org lock is still held. Commit
    # before any irreversible cleanup, so late inserts cannot lose references
    # after cleanup's inventory has been read. Retries never downgrade final.
    await db.execute(
        update(OrgClosure)
        .where(OrgClosure.organization_id == org)
        .values(source=ORGANIZATION_DELETE)
    )
    await db.commit()

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
                select(CarrierVerification)
                .with_for_update()
                .where(CarrierVerification.organization_id == org)
            )
        )
        .scalars()
        .all()
    )
    service_ids: dict[str, set[str]] = {}
    await check_pending(db, org)
    previous_states = {row.id: row.state for row in verifications}
    for row in verifications:
        if registry(row.provider, row.purpose) is None and (
            row.provider_refs or row.state == "approved"
        ):
            raise HTTPException(
                503,
                "Registration cleanup is unavailable. Contact support, then retry deletion.",
            )
    # Disarm waiting workers under the same row locks they use to claim work.
    # Keep every provider reference until the final organization purge.
    for row in verifications:
        row.state = "cancelled"
        row.cancelled_at = row.cancelled_at or datetime.now(timezone.utc)
    await db.commit()
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
        if provider is None and (
            row.provider_refs or previous_states[row.id] == "approved"
        ):
            raise HTTPException(
                503,
                "Registration cleanup is unavailable. Contact support, then retry deletion.",
            )
        if previous_states[row.id] == "approved" and row.purpose == "sms":
            await _rate_before_wipe(db, row, provider)
            await _detach_registration(db, row, provider)
        if provider is not None:
            if row.provider == "didww":
                await provider.discard_organization(
                    row.provider_refs, organization_id=str(org)
                )
            elif row.provider == "telnyx" and row.purpose == "sms":
                # A cancelled filing can retain a brand reused by a later
                # campaign. Clear every campaign before deleting any brand.
                await provider.discard(
                    {"campaign_id": row.provider_refs.get("campaign_id")}, strict=True
                )
            else:
                await provider.discard(row.provider_refs, strict=True)
            row.state = "cancelled"
            # Retain retry IDs until the website's final purge commits. A
            # later cleanup step can fail after this cancellation succeeds.
            row.cancelled_at = datetime.now(timezone.utc)
            await db.commit()

    for row in verifications:
        if row.provider == "telnyx" and row.purpose == "sms":
            provider = registry(row.provider, row.purpose)
            if provider is not None:
                await provider.discard(
                    {
                        key: row.provider_refs[key]
                        for key in ("brand_id", "kept_brand_id")
                        if row.provider_refs.get(key)
                    },
                    strict=True,
                )

    # DIDWW reconciles every filing's org-owned identity after its address
    # deletion. The last reused address therefore also releases the identity.

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
        (
            await db.execute(
                select(Call)
                .where(Call.organization_id == org)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for call in calls:
        # A terminal call's egress may still be writing audio. Keep its row
        # until that process ends, rather than lose the erasure tombstone.
        await ensure_recording_stopped(call, lk)
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
        # One array bind keeps arbitrarily large inventories below asyncpg's
        # argument limit. Reuse the same inventory in each ownership lookup.
        storage_keys = bindparam("storage_keys", list(keys), type_=ARRAY(Text))
        shared_raw = (
            (
                await db.execute(
                    select(Email.raw_s3_key).where(
                        Email.organization_id != org,
                        Email.raw_s3_key == any_(storage_keys),
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
                        Email.organization_id != org,
                        EmailAttachment.s3_key == any_(storage_keys),
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
                        EmailAttachmentUpload.s3_key == any_(storage_keys),
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
