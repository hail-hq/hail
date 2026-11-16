"""Account close: release the org's numbers and cancel its registrations.

When hail-website deletes an account, the org gets an ``org_closures`` row
(``hailhq.core.models.OrgClosure``). For every org closed by ``now``, this
step:

1. releases each number that is not released yet, through
   ``release_org_number`` (the same code as ``DELETE /numbers/{id}`` and the
   dunning release). A pending order is left to the order reconciler and
   released on a later pass, once the carrier answered. A failed order
   whose carrier number was not given back (``unreleased_resource_id``) is
   given back here too (``retry_release``), so a final refusal is stored
   and the registrations it blocks do not wait for good.
2. cancels each carrier registration (number verifications, US texting
   brands and campaigns, toll-free verifications) through ``cancel_locked``
   (the same code as ``DELETE /verifications/{id}``), in any state, with
   the whole filing discarded (no brand kept for a next filing). A
   registration waits while a number it covers is not released: the carrier
   refuses to delete a bundle or address a live number uses, and the cancel
   wipes the ids the number's release needs.

Run by ``POST /internal/org-closures`` (that org, after the response) and by
every verification worker pass (every closed org), before
``clear_closed_org_registrations``. Safe to run again: released numbers and
cancelled rows are skipped. One item's failure never stops the others; it is
tried again on the next pass. A carrier delete that failed (``discard``
raised: an outage, a timeout, a 429) keeps the registration and its carrier
ids, so the next pass deletes it again. A carrier refusal a retry does not change
(``final_refusal``) is stored and the item is not tried again:
``provisioning_metadata.release_refused`` on the number,
``provider_refs.close_refused`` on the registration, both
``{"status": <HTTP status>, "at": <ISO time>}``.

No money moves: a release writes no ledger row, the cancel does not call the
website's fee rater (``_rate_before_wipe``, ``_notify_billing``), and the
hold return finds no hold once hail-website deleted the org's ledger. No
audit row is written: hail-website deleted the org's audit log. Logs carry
ids and counts only.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from uuid import UUID

from hailhq.api.number_orders import retry_release
from hailhq.api.routes.numbers import release_org_number
from hailhq.api.routes.verifications import Registry, cancel_locked
from hailhq.core.carrier_routing import CARRIERS, final_refusal
from hailhq.core.db import org_lock
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import CarrierVerification, PhoneNumber
from hailhq.core.retention import closed_org_ids
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

__all__ = ["CloseCounts", "close_org_carrier_items"]

RELEASE_REFUSED = "release_refused"
CLOSE_REFUSED = "close_refused"


@dataclass
class CloseCounts:
    released: int = 0
    cancelled: int = 0
    # Failed this pass; tried again on the next one.
    failed: int = 0
    # Refused for good by the carrier; not tried again.
    refused: int = 0
    # Registrations waiting for a number of theirs to be released.
    waiting: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _refusal(status: int | None, now: datetime) -> dict:
    return {"status": status, "at": now.isoformat()}


def _unreleased(org_ids):
    """Numbers of the closed orgs a carrier may still hold: active, pending,
    or failed with a carrier number not given back yet. A number refused
    for good counts as done."""
    return and_(
        PhoneNumber.organization_id.in_(org_ids),
        or_(
            PhoneNumber.provisioning_state.in_(("active", "pending")),
            PhoneNumber.provisioning_metadata.has_key("unreleased_resource_id"),
        ),
        ~PhoneNumber.provisioning_metadata.has_key(RELEASE_REFUSED),
    )


async def _release_numbers(
    db: AsyncSession,
    org_ids,
    now: datetime,
    lk: LiveKitClient | None,
    counts: CloseCounts,
) -> None:
    rows = (
        await db.execute(
            select(PhoneNumber.id, PhoneNumber.organization_id).where(
                PhoneNumber.organization_id.in_(org_ids),
                ~PhoneNumber.provisioning_metadata.has_key(RELEASE_REFUSED),
                or_(
                    PhoneNumber.provisioning_state == "active",
                    # A failed order: release_org_number only dismisses it.
                    and_(
                        PhoneNumber.provisioning_state == "failed",
                        PhoneNumber.released_at.is_(None),
                    ),
                    # A failed order whose carrier number was not given back.
                    PhoneNumber.provisioning_metadata.has_key("unreleased_resource_id"),
                ),
            )
        )
    ).all()
    await db.commit()
    for number_id, org in rows:
        try:
            number = await db.get(PhoneNumber, number_id)
            if number is None:
                continue
            if number.provider not in CARRIERS:
                # release_org_number refuses it (409) on every try.
                await _store_release_refusal(db, number_id, None, now)
                counts.refused += 1
                logger.warning(
                    "account close: number %s (org %s) is on a carrier Hail "
                    "cannot release; release it by hand",
                    number_id,
                    org,
                )
                continue
            if number.provisioning_state == "failed" and (
                number.provisioning_metadata or {}
            ).get("unreleased_resource_id"):
                # Gives the carrier number back; a refusal is stored below.
                await retry_release(db, number)
            await release_org_number(db, number, lk)
            counts.released += 1
        except Exception as exc:
            await db.rollback()
            status = final_refusal(exc)
            if status is not None:
                await _store_release_refusal(db, number_id, status, now)
                counts.refused += 1
                logger.warning(
                    "account close: carrier refused to release number %s "
                    "(org %s, HTTP %s); not retried",
                    number_id,
                    org,
                    status,
                )
            else:
                counts.failed += 1
                logger.warning(
                    "account close: release of number %s (org %s) failed "
                    "(%s); retried next pass",
                    number_id,
                    org,
                    type(exc).__name__,
                )


async def _store_release_refusal(
    db: AsyncSession, number_id: UUID, status: int | None, now: datetime
) -> None:
    try:
        number = await db.get(PhoneNumber, number_id)
        if number is None:
            return
        await db.refresh(number)
        number.provisioning_metadata = {
            **(number.provisioning_metadata or {}),
            RELEASE_REFUSED: _refusal(status, now),
        }
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("account close: could not store refusal of %s", number_id)


async def _waits_for_a_number(db: AsyncSession, row: CarrierVerification) -> bool:
    """True while a number this registration covers is not released. A
    number verification covers the org's numbers of its carrier, country
    and type (older numbers do not name the verification they used); an
    SMS registration covers the numbers on it and its toll-free number."""
    live = _unreleased([row.organization_id])
    if row.purpose == "number":
        tie = and_(
            PhoneNumber.provider == row.provider,
            PhoneNumber.country_code == row.country_code,
            PhoneNumber.number_type == row.number_type,
        )
    else:
        tie = or_(
            PhoneNumber.sms_registration_id == row.id,
            PhoneNumber.id == row.number_id,
            PhoneNumber.provisioning_metadata["offer"]["verification_row_id"].astext
            == str(row.id),
        )
    found = await db.scalar(select(PhoneNumber.id).where(live, tie).limit(1))
    return found is not None


async def _cancel_registrations(
    db: AsyncSession, org_ids, now: datetime, registry: Registry, counts: CloseCounts
) -> None:
    ids = (
        (
            await db.execute(
                select(CarrierVerification.id).where(
                    CarrierVerification.organization_id.in_(org_ids),
                    or_(
                        CarrierVerification.state != "cancelled",
                        # A brand an earlier dismissal kept for a next filing.
                        CarrierVerification.provider_refs.has_key("kept_brand_id"),
                    ),
                    ~CarrierVerification.provider_refs.has_key(CLOSE_REFUSED),
                )
            )
        )
        .scalars()
        .all()
    )
    await db.commit()
    for verification_id in ids:
        try:
            row = await db.get(CarrierVerification, verification_id)
            if row is None:
                continue
            await org_lock(db, row.organization_id)
            await db.refresh(row, with_for_update=True)
            refs = row.provider_refs or {}
            if CLOSE_REFUSED in refs or (
                row.state == "cancelled" and "kept_brand_id" not in refs
            ):
                await db.commit()
                continue
            if await _waits_for_a_number(db, row):
                await db.commit()
                counts.waiting += 1
                continue
            provider = registry(row.provider, row.purpose)
            if provider is None:
                # Carrier not configured here: cancelling now would drop
                # the ids while the carrier still holds the filing.
                await db.commit()
                counts.failed += 1
                logger.warning(
                    "account close: carrier %s not configured; verification "
                    "%s kept for the next pass",
                    row.provider,
                    row.id,
                )
                continue
            if row.state == "cancelled":
                await provider.discard({"brand_id": refs["kept_brand_id"]})
                row.provider_refs = {
                    k: v for k, v in refs.items() if k != "kept_brand_id"
                }
                flag_modified(row, "provider_refs")
            else:
                await cancel_locked(db, row, provider, keep_brand=False)
            await db.commit()
            counts.cancelled += 1
        except Exception as exc:
            await db.rollback()
            status = final_refusal(exc)
            if status is None:
                counts.failed += 1
                logger.warning(
                    "account close: cancel of verification %s failed (%s); "
                    "retried next pass",
                    verification_id,
                    type(exc).__name__,
                )
                continue
            counts.refused += 1
            logger.warning(
                "account close: carrier refused to cancel verification %s "
                "(HTTP %s); not retried",
                verification_id,
                status,
            )
            try:
                row = await db.get(CarrierVerification, verification_id)
                if row is not None:
                    await db.refresh(row, with_for_update=True)
                    row.provider_refs = {
                        **(row.provider_refs or {}),
                        CLOSE_REFUSED: _refusal(status, now),
                    }
                    await db.commit()
            except Exception:
                await db.rollback()
                logger.exception(
                    "account close: could not store refusal of %s", verification_id
                )


async def close_org_carrier_items(
    db: AsyncSession,
    now: datetime,
    registry: Registry,
    lk: LiveKitClient | None,
    organization_id: UUID | None = None,
) -> CloseCounts:
    """Release the numbers, then cancel the registrations, of every org
    closed by ``now`` (one org when ``organization_id`` is given). See the
    module docstring. Commits per item. Never raises for one item."""
    counts = CloseCounts()
    # A subquery, not a list: org_closures only grows.
    org_ids = closed_org_ids(now, organization_id)
    await _release_numbers(db, org_ids, now, lk, counts)
    await _cancel_registrations(db, org_ids, now, registry, counts)
    if any(counts.as_dict().values()):
        logger.info("account close: %s", counts.as_dict())
    return counts
