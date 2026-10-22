"""Put an organization's US long codes on its approved SMS registration, and
take them off again. ``PhoneNumber.sms_registration_id`` records which
numbers are on which registration; the send gate reads it."""

from __future__ import annotations

import logging
from uuid import UUID

from hailhq.core.models import CarrierVerification, PhoneNumber
from hailhq.core.providers.verification.base import SmsRegistrationProvider
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def registered_numbers(
    db: AsyncSession, row: CarrierVerification
) -> list[PhoneNumber]:
    """The org's active US long codes on the row's carrier that belong on the
    registration, oldest first. A person's registration takes one number:
    the one already on it, else the oldest number on no registration."""
    numbers = (
        (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.organization_id == row.organization_id,
                    PhoneNumber.provider == row.provider,
                    PhoneNumber.provisioning_state == "active",
                    PhoneNumber.country_code == "US",
                    PhoneNumber.number_type == "local",
                    PhoneNumber.capabilities.any("sms"),
                    PhoneNumber.provider_resource_id.is_not(None),
                )
                .order_by(
                    PhoneNumber.acquired_at.nulls_last(),
                    PhoneNumber.created_at,
                    PhoneNumber.id,
                )
            )
        )
        .scalars()
        .all()
    )
    if row.subject_type == "person":
        on = [n for n in numbers if n.sms_registration_id == row.id]
        free = [n for n in numbers if n.sms_registration_id is None]
        return on[:1] or free[:1]
    return list(numbers)


async def _attach(
    row: CarrierVerification, provider: SmsRegistrationProvider, number: PhoneNumber
) -> bool:
    """Attach one number and record it. A failure is logged with the number
    id. True when the number is now on the registration."""
    try:
        await provider.attach_number(
            row.provider_refs,
            provider_resource_id=number.provider_resource_id,
            e164=number.e164,
        )
    except Exception:
        logger.warning(
            "SMS registration attach failed for number %s", number.id, exc_info=True
        )
        return False
    number.sms_registration_id = row.id
    service = (row.provider_refs or {}).get("messaging_service_sid")
    if service:
        number.messaging_service_sid = service
    return True


async def attach_org_numbers(
    db: AsyncSession, row: CarrierVerification, provider: SmsRegistrationProvider
) -> int:
    """Attach every number that belongs on the registration, each on its
    own: one failure does not stop the rest. Returns how many were
    attached."""
    attached = 0
    for number in await registered_numbers(db, row):
        attached += await _attach(row, provider, number)
    return attached


async def detach_org_numbers(
    db: AsyncSession, row: CarrierVerification, provider: SmsRegistrationProvider
) -> list[PhoneNumber]:
    """Take every number off the registration. Returns the numbers that
    left their messaging service; the caller sets them up again."""
    numbers = (
        (
            await db.execute(
                select(PhoneNumber).where(PhoneNumber.sms_registration_id == row.id)
            )
        )
        .scalars()
        .all()
    )
    moved = []
    for number in numbers:
        if await detach_number(row, provider, number):
            moved.append(number)
    return moved


async def detach_number(
    row: CarrierVerification, provider: SmsRegistrationProvider, number: PhoneNumber
) -> bool:
    """Take one number off the registration. True when it also left its
    messaging service (``messaging_service_sid`` is then cleared)."""
    left_service = False
    if number.provider_resource_id is not None:
        left_service = await provider.detach_number(
            row.provider_refs or {},
            provider_resource_id=number.provider_resource_id,
            e164=number.e164,
        )
    number.sms_registration_id = None
    if left_service:
        number.messaging_service_sid = None
    return left_service


async def approved_registration(
    db: AsyncSession, organization_id: UUID, provider: str
) -> CarrierVerification | None:
    return (
        await db.execute(
            select(CarrierVerification)
            .where(
                CarrierVerification.organization_id == organization_id,
                CarrierVerification.provider == provider,
                CarrierVerification.purpose == "sms",
                CarrierVerification.state == "approved",
            )
            # carrier_verifications_sms_live_uniq allows one live row per
            # (org, carrier, country, type); limit(1) is defensive.
            .order_by(CarrierVerification.approved_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def attach_if_registered(db: AsyncSession, number: PhoneNumber, registry) -> None:
    """A newly active US long code joins the org's approved registration, when
    there is one. Failures are logged; the number still works for voice."""
    if number.country_code != "US" or number.number_type != "local":
        return
    if number.provider_resource_id is None or number.organization_id is None:
        return
    row = await approved_registration(db, number.organization_id, number.provider)
    if row is None:
        return
    provider = registry(number.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return
    if number.id not in {n.id for n in await registered_numbers(db, row)}:
        return  # a person's registration holds one number only
    await _attach(row, provider, number)
