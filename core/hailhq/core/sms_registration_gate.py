"""Whether a text may leave a US long code for a US number: only when that
number is on an approved US SMS registration of its organization."""

from __future__ import annotations

from uuid import UUID

import phonenumbers
from hailhq.core.models import CarrierVerification, PhoneNumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# US domestic NANP territories share the US texting registration. Campaigns
# retain country US; number ownership keeps the actual ISO territory code.
US_SMS_COUNTRY_CODES = frozenset({"US", "AS", "GU", "MP", "PR", "VI"})

BLOCK_MESSAGE = (
    "US texting from this number needs SMS registration. Open Numbers in the "
    "console and choose Register for US texting."
)


def is_us(e164: str) -> bool:
    """US mainland and territory destinations need domestic registration.
    An unknown +1 area code counts as US, so the gate errs toward blocking."""
    if not e164.startswith("+1"):
        return False
    try:
        region = phonenumbers.region_code_for_number(phonenumbers.parse(e164))
    except phonenumbers.NumberParseException:
        return True
    return region is None or region in US_SMS_COUNTRY_CODES


def approved_registration_ids(organization_id: UUID):
    """Subquery: ids of the org's approved US SMS registrations."""
    return select(CarrierVerification.id).where(
        CarrierVerification.organization_id == organization_id,
        CarrierVerification.purpose == "sms",
        CarrierVerification.state == "approved",
    )


async def on_approved_registration(
    db: AsyncSession, organization_id: UUID, number: PhoneNumber | None
) -> bool:
    """True when ``number`` is on an approved US SMS registration of the
    org."""
    if number is None or number.sms_registration_id is None:
        return False
    found = (
        await db.execute(
            approved_registration_ids(organization_id).where(
                CarrierVerification.id == number.sms_registration_id,
                CarrierVerification.provider == number.provider,
                CarrierVerification.number_type == number.number_type,
                *(
                    (CarrierVerification.number_id == number.id,)
                    if number.number_type == "toll_free"
                    else ()
                ),
            )
        )
    ).scalar_one_or_none()
    return found is not None


async def us_registration_block(
    db: AsyncSession, organization_id: UUID, from_number: PhoneNumber, to_e164: str
) -> str | None:
    if from_number.number_type == "toll_free" and from_number.provider != "twilio":
        return None  # Other carriers retain their existing carrier-side gate.
    if from_number.number_type == "toll_free" and from_number.country_code in {
        "US",
        "CA",
    }:
        try:
            destination = phonenumbers.region_code_for_number(
                phonenumbers.parse(to_e164)
            )
        except phonenumbers.NumberParseException:
            destination = None
        if not (is_us(to_e164) or destination == "CA"):
            return None
        if await on_approved_registration(db, organization_id, from_number):
            return None
        if (
            from_number.organization_id == organization_id
            and from_number.provisioning_state == "active"
            and from_number.provider_resource_id
        ):
            from hailhq.core.carrier_routing import sms_route

            try:
                provider = sms_route(from_number.provider)
                if await provider.tollfree_approved(
                    provider_resource_id=from_number.provider_resource_id,
                    e164=from_number.e164,
                ):
                    return None
            except Exception:
                pass  # No positive proof: keep the send blocked.
        return "Toll-free texting to the US and Canada needs verification for this number. Open Numbers in the console and choose Register for texting."
    if from_number.country_code not in US_SMS_COUNTRY_CODES or not is_us(to_e164):
        return None
    if await on_approved_registration(db, organization_id, from_number):
        return None
    return BLOCK_MESSAGE


async def registered_sms_number(
    db: AsyncSession, organization_id: UUID
) -> PhoneNumber | None:
    """The org's oldest active number that can text and is on an approved
    US SMS registration. POST /sms without ``from`` prefers it for a US
    destination."""
    return (
        await db.execute(
            select(PhoneNumber)
            .where(
                PhoneNumber.organization_id == organization_id,
                PhoneNumber.provisioning_state == "active",
                PhoneNumber.capabilities.any("sms"),
                PhoneNumber.sms_registration_id.in_(
                    approved_registration_ids(organization_id)
                ),
            )
            .order_by(PhoneNumber.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
