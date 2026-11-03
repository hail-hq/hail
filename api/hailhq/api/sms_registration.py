"""Put an organization's US long codes on its approved SMS registration, and
take them off again. ``PhoneNumber.sms_registration_id`` records which
numbers are on which registration; the send gate reads it.

A number the carrier refuses keeps its retry state in
``provisioning_metadata['sms_attach_refused']``: the registration, the wait,
when the background pass may try it again, and a hash of the carrier's last
audited reason. A person's registration then moves on to their next number.
When the carrier already holds another of the organization's free numbers
on a person's registration, Hail records that number as attached instead."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from hailhq.api.audit import write_audit_log
from hailhq.core.db import org_lock
from hailhq.core.models import CarrierVerification, PhoneNumber
from hailhq.core.providers.verification.base import (
    RegistrationHoldsAnotherNumber,
    SmsRegistrationProvider,
)
from hailhq.core.sms_registration_gate import US_SMS_COUNTRY_CODES
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_REFUSED = "sms_attach_refused"
# The background pass waits this long after a refusal, doubling each time.
_FIRST_WAIT_HOURS = 1
_MAX_WAIT_HOURS = 24

# What makes a number eligible for a US SMS registration.
_ELIGIBLE_NUMBER = (
    PhoneNumber.provisioning_state == "active",
    PhoneNumber.country_code.in_(US_SMS_COUNTRY_CODES),
    PhoneNumber.number_type == "local",
    PhoneNumber.capabilities.any("sms"),
    PhoneNumber.provider_resource_id.is_not(None),
)


async def registered_numbers(
    db: AsyncSession, row: CarrierVerification
) -> list[PhoneNumber]:
    """The org's active US long codes on the row's carrier that belong on the
    registration, oldest first. A person's registration takes one number:
    the one already on it (see ``_has_number``), and none when that number
    is no longer eligible. Else the free numbers (on no registration) up to
    and including the oldest one the carrier has not refused for this
    registration, so a refused number does not block the next one. Callers
    stop at the first number that attaches."""
    numbers = (
        (
            await db.execute(
                select(PhoneNumber)
                .where(
                    PhoneNumber.organization_id == row.organization_id,
                    PhoneNumber.provider == row.provider,
                    *_ELIGIBLE_NUMBER,
                )
                .order_by(
                    PhoneNumber.acquired_at.nulls_last(),
                    PhoneNumber.created_at,
                    PhoneNumber.id,
                )
                # Read what other sessions committed, so a person's
                # registration never gets two. The worker and
                # attach_if_registered's callers hold the org lock. The
                # approval (_settle_sms_approval) holds only the row lock:
                # the row turns approved in the same commit as its numbers,
                # and the other paths act only on approved rows, so none
                # attaches to it before that commit.
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    if row.subject_type == "person":
        if await _has_number(db, row):
            # Only a still-eligible number on it is tried again (a re-attach).
            return [n for n in numbers if n.sms_registration_id == row.id][:1]
        free = [n for n in numbers if n.sms_registration_id is None]
        for i, number in enumerate(free):
            if _refusal(row, number) is None:
                return free[: i + 1]
        return free
    return list(numbers)


def _refusal(row: CarrierVerification, number: PhoneNumber) -> dict | None:
    """The number's retry state for this registration, if it was refused.
    State left by another registration does not count."""
    refusal = number.provisioning_metadata.get(_REFUSED)
    if refusal and refusal["registration_id"] == str(row.id):
        return refusal
    return None


def _waiting(row: CarrierVerification, number: PhoneNumber) -> bool:
    """True while the background pass must leave a refused number alone."""
    refusal = _refusal(row, number)
    return refusal is not None and datetime.now(timezone.utc) < (
        datetime.fromisoformat(refusal["retry_at"])
    )


async def _record_refusal(
    row: CarrierVerification, number: PhoneNumber, reason: str
) -> None:
    """Push the number's next background try back. The first refusal, and
    each one whose carrier reason differs from the last audited one, also
    writes the audit row that says why the number's texts fail."""
    refusal = _refusal(row, number)
    hours = _FIRST_WAIT_HOURS
    if refusal:
        hours = min(refusal["wait_hours"] * 2, _MAX_WAIT_HOURS)
    # A hash, not the text: the audit row already holds the text.
    reason_hash = hashlib.sha256(reason.encode()).hexdigest()[:16]
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        _REFUSED: {
            "registration_id": str(row.id),
            "wait_hours": hours,
            "retry_at": (
                datetime.now(timezone.utc) + timedelta(hours=hours)
            ).isoformat(),
            "reason_hash": reason_hash,
        },
    }
    # State written before reason_hash existed has no key: audit once more.
    if refusal is None or refusal.get("reason_hash") != reason_hash:
        await write_audit_log(
            row.organization_id,
            None,
            "verification.sms_attach_refused",
            "phone_number",
            number.id,
            {
                "e164": number.e164,
                "verification_id": str(row.id),
                "carrier_reason": reason,
            },
            actor_kind="system",
        )


async def _attach(
    db: AsyncSession,
    row: CarrierVerification,
    provider: SmsRegistrationProvider,
    number: PhoneNumber,
) -> PhoneNumber | None:
    """Attach one number and record it. A failure is logged with the number
    id and recorded (see ``_record_refusal``). Returns the number now on the
    registration: this one, or the free number of this organization the
    carrier already holds on a person's registration (an earlier link that
    timed out may have gone through). None when nothing joined."""
    try:
        await provider.attach_number(
            row.provider_refs,
            provider_resource_id=number.provider_resource_id,
            e164=number.e164,
        )
    except RegistrationHoldsAnotherNumber as exc:
        # A number already on the registration in Hail stays its one number
        # (a re-attach): the carrier's other number is refused, not added.
        held = None
        if not await _has_number(db, row):
            held = await _free_number(db, row, exc.e164)
        if held is not None:
            logger.info(
                "SMS registration %s: the carrier holds number %s, recorded",
                row.id,
                held.id,
            )
            _record_attached(row, held)
            # The number tried is not refused: drop its wait for this row.
            if _refusal(row, number) is not None:
                _clear_refusal(number)
            return held
        await _refused(row, number, exc)
        return None
    except Exception as exc:
        await _refused(row, number, exc)
        return None
    _record_attached(row, number)
    return number


async def _refused(
    row: CarrierVerification, number: PhoneNumber, exc: Exception
) -> None:
    logger.warning(
        "SMS registration attach failed for number %s", number.id, exc_info=exc
    )
    # A failed re-attach leaves a number on the registration: it still texts.
    if number.sms_registration_id != row.id:
        await _record_refusal(row, number, str(exc)[:500] or repr(exc))


async def _has_number(db: AsyncSession, row: CarrierVerification) -> bool:
    """True when any number is on the registration (read fresh), eligible
    or not: a released number still recorded on it counts. The one rule for
    "a person's registration has its number" (``registered_numbers`` and the
    held-number check in ``_attach``), so Hail never records a second one.
    Such a registration takes no other number until support clears that
    number's ``sms_registration_id``."""
    return (
        await db.execute(
            select(PhoneNumber.id)
            .where(PhoneNumber.sms_registration_id == row.id)
            .limit(1)
        )
    ).first() is not None


async def _free_number(
    db: AsyncSession, row: CarrierVerification, e164: str
) -> PhoneNumber | None:
    """The organization's eligible number ``e164`` on the row's carrier, when
    it is on no registration. The row is locked (``FOR UPDATE``) until the
    caller commits; Postgres checks these conditions again on the newest
    version of the row once the lock is free, so a release or a move that
    committed first is seen. The worker and ``attach_if_registered``'s
    callers also hold the org lock. The approval (``_settle_sms_approval``)
    does not: it holds the verification row lock, and a path that takes
    the org lock and then writes a verification row (``cancel_verification``)
    takes the two in the other order, so the org lock here could deadlock."""
    return (
        await db.execute(
            select(PhoneNumber)
            .where(
                PhoneNumber.organization_id == row.organization_id,
                PhoneNumber.provider == row.provider,
                PhoneNumber.e164 == e164,
                PhoneNumber.sms_registration_id.is_(None),
                *_ELIGIBLE_NUMBER,
            )
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _record_attached(row: CarrierVerification, number: PhoneNumber) -> None:
    number.sms_registration_id = row.id
    service = (row.provider_refs or {}).get("messaging_service_sid")
    if service:
        number.messaging_service_sid = service
    _clear_refusal(number)


def _clear_refusal(number: PhoneNumber) -> None:
    if _REFUSED in number.provisioning_metadata:
        number.provisioning_metadata = {
            k: v for k, v in number.provisioning_metadata.items() if k != _REFUSED
        }


async def attach_org_numbers(
    db: AsyncSession,
    row: CarrierVerification,
    provider: SmsRegistrationProvider,
    *,
    background: bool = False,
) -> tuple[int, int]:
    """Attach every number that belongs on the registration, each on its
    own: one failure does not stop the rest. A person's registration stops
    at the first number that attaches. The background pass skips numbers
    already on the registration and numbers in their wait. Returns how many
    attached and how many failed."""
    attached = failed = 0
    for number in await registered_numbers(db, row):
        if background and (
            number.sms_registration_id is not None or _waiting(row, number)
        ):
            continue
        if await _attach(db, row, provider, number) is not None:
            attached += 1
            if row.subject_type == "person":
                break
        else:
            failed += 1
    return attached, failed


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
    if number.country_code not in US_SMS_COUNTRY_CODES or number.number_type != "local":
        return
    if number.provider_resource_id is None or number.organization_id is None:
        return
    row = await approved_registration(db, number.organization_id, number.provider)
    if row is None:
        return
    provider = registry(number.provider, "sms")
    if not isinstance(provider, SmsRegistrationProvider):
        return
    # registered_numbers re-reads every listed number under the org lock,
    # this one included: the background pass may have changed it while this
    # request waited for the lock.
    if number.id not in {n.id for n in await registered_numbers(db, row)}:
        # A person's registration holds one number: this one is not next.
        return
    joined = await _attach(db, row, provider, number)
    if joined is not None and joined.id != number.id:
        # The carrier held another of the org's numbers: the approval and
        # the worker write this row for theirs. It names both numbers, so
        # support sees why the number tried did not join.
        await write_audit_log(
            row.organization_id,
            None,
            "verification.sms_attached",
            "carrier_verification",
            row.id,
            {"numbers": 1, "failed": 0, "adopted": joined.e164, "tried": number.e164},
            actor_kind="system",
        )


async def reconcile_approved_registrations(db: AsyncSession, registry) -> int:
    """Repair missing campaign links, including numbers held before territory support.

    Only an existing approved campaign is used; this never files a new brand
    or campaign. The org lock serializes repair with cancellation and release.
    A refused number is skipped until its wait ends; ``attach_if_registered``
    (POST /numbers/{id}/enable-sms) does not wait. A person's registration
    moves on to their next free number while the older ones are refused.
    """
    missing_number = (
        select(PhoneNumber.id)
        .where(
            PhoneNumber.organization_id == CarrierVerification.organization_id,
            PhoneNumber.provider == CarrierVerification.provider,
            PhoneNumber.sms_registration_id.is_(None),
            *_ELIGIBLE_NUMBER,
        )
        .exists()
    )
    ids = (
        (
            await db.execute(
                select(CarrierVerification.id).where(
                    CarrierVerification.purpose == "sms",
                    CarrierVerification.state == "approved",
                    missing_number,
                )
            )
        )
        .scalars()
        .all()
    )
    total = 0
    for registration_id in ids:
        try:
            row = await db.get(CarrierVerification, registration_id)
            if row is None:
                continue
            await org_lock(db, row.organization_id)
            await db.refresh(row)
            if row.state != "approved":
                await db.commit()
                continue
            provider = registry(row.provider, "sms")
            attached = failed = 0
            if isinstance(provider, SmsRegistrationProvider):
                attached, failed = await attach_org_numbers(
                    db, row, provider, background=True
                )
            organization_id, verification_id = row.organization_id, row.id
            await db.commit()
            total += attached
            # _attach audits a refused number (again when its reason
            # changes); this row counts the numbers that moved.
            if attached:
                await write_audit_log(
                    organization_id,
                    None,
                    "verification.sms_attached",
                    "carrier_verification",
                    verification_id,
                    {"numbers": attached, "failed": failed},
                    actor_kind="system",
                )
        except Exception:
            await db.rollback()
            logger.exception(
                "SMS campaign repair failed for registration %s", registration_id
            )
    return total
