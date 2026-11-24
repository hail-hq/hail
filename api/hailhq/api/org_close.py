"""Account close: release the org's numbers and cancel its registrations.

When hail-website deletes an account, the org gets an ``org_closures`` row
(``hailhq.core.models.OrgClosure``). For every org closed by ``now``, this
step:

1. releases each number that is not released yet, through
   ``release_org_number`` (the same code as ``DELETE /numbers/{id}`` and the
   dunning release). A pending order is left to the order reconciler and
   released on a later pass, once the carrier answered. A failed order
   whose carrier number was not given back (``unreleased_resource_id``) is
   given back here too (``retry_release``), so a final refusal is stored.
2. removes each connected phone system number (``pbx``) from its trunk and
   marks it released, then deletes each connection (both trunks), through
   ``remove_connected_number`` and ``delete_connection``. No carrier is
   called. Without LiveKit, or when LiveKit fails, the item counts as
   failed and is retried on the next pass; it is never stored as a refusal.
3. cancels each carrier registration (number verifications, US texting
   brands and campaigns, toll-free verifications) through ``cancel_locked``
   (the same code as ``DELETE /verifications/{id}``), in any state, with
   the whole filing discarded (no brand kept for a next filing). A
   registration waits while a number it covers is not released: the carrier
   refuses to delete a bundle or address a live number uses, and the cancel
   wipes the ids the number's release needs. When the carrier refused that
   number's release, the registration is not cancelled either: it is marked
   ``close_refused`` with the ``number_id``, its ids kept for ops.

Run by ``POST /internal/org-closures`` (that org, after the response) and by
every verification worker pass (every closed org), before
``clear_closed_org_registrations``. Safe to run again: released numbers and
cancelled rows are skipped. One item's failure never stops the others.

A failed release or cancel keeps the number or registration and its carrier
ids. How it counts (``carrier_routing.failure_kind``):

* an outage, a timeout, a lost connection, a 401 (Hail's credentials), a
  408, 425 or 429, or a database error: tried again on the next pass, with
  no limit;
* a 403 or 409 (the carrier refuses this record for now), or a carrier
  step that failed with no carrier answer (``DiscardRefused``, a bug):
  tried again for ``CLOSE_RETRY_FOR`` from the first such failure
  (``close_failing_since``), then stored as a refusal;
* any other 4xx: stored as a refusal at once.

A refusal is stored as ``provisioning_metadata.release_refused`` on the
number and ``provider_refs.close_refused`` on the registration, both
``{"status": <HTTP status or null>, "at": <ISO time>}``, and the item is not
tried again. ``close_failing_since`` is cleared by a success and by any
failure that does not count, so the window only measures refusals that keep
coming.

No money moves: a release writes no ledger row, the cancel does not call the
website's fee rater (``_rate_before_wipe``, ``_notify_billing``), and the
hold return finds no hold once hail-website deleted the org's ledger. No
audit row is written: hail-website deleted the org's audit log. Logs carry
ids and counts only.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from uuid import UUID

from hailhq.api.connected_numbers import delete_connection, remove_connected_number
from hailhq.api.number_orders import retry_release
from hailhq.api.routes.numbers import release_org_number
from hailhq.api.routes.verifications import Registry, cancel_locked
from hailhq.core.carrier_routing import CARRIERS, carrier_http_status, failure_kind
from hailhq.core.db import org_lock
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import CarrierVerification, PhoneNumber, SipConnection
from hailhq.core.providers.verification.base import (
    DiscardRefused,
    counted_discard,
)
from hailhq.core.retention import closed_org_ids
from hailhq.core.sms_trial import registration_trial_number
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

logger = logging.getLogger(__name__)

__all__ = ["CloseCounts", "close_org_carrier_items"]

RELEASE_REFUSED = "release_refused"
CLOSE_REFUSED = "close_refused"
# When the carrier first answered a 4xx a retry may change; see the module
# docstring. On the number's provisioning_metadata or the registration's
# provider_refs.
FAILING_SINCE = "close_failing_since"
CLOSE_RETRY_FOR = timedelta(days=7)


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


async def _counts_as_refused(
    db: AsyncSession, model, item_id: UUID, column: str, exc: BaseException, now
) -> tuple[bool, int | None]:
    """``(refused, status)`` for a failed release or cancel: refused True
    when the item is marked and not tried again. ``status`` is the
    carrier's HTTP status (None for none). See the module docstring:

    * a final 4xx: refused at once;
    * a 403 or 409, or a carrier step that failed with no carrier answer
      (``DiscardRefused``, a bug): refused once such failures have kept
      coming for ``CLOSE_RETRY_FOR``; the first one's time is stored
      (``FAILING_SINCE``);
    * anything else (an outage, a 401, 408, 425 or 429, a database error):
      retried with no limit, and ``FAILING_SINCE`` is cleared, so the window
      only measures refusals that keep coming."""
    status = carrier_http_status(exc)
    kind = failure_kind(
        exc, unknown="refused" if isinstance(exc, DiscardRefused) else "outage"
    )
    if kind == "final":
        return True, status
    try:
        item = await db.get(model, item_id)
        if item is None:
            return False, status
        if kind != "refused" and FAILING_SINCE not in (getattr(item, column) or {}):
            # Nothing to clear: skip the row lock and the commit.
            await db.rollback()
            return False, status
        await db.refresh(item, with_for_update=True)
        data = getattr(item, column) or {}
        since = data.get(FAILING_SINCE)
        refused = False
        if kind != "refused":
            if since is not None:
                setattr(item, column, _without_since(data))
        elif since is None:
            setattr(item, column, {**data, FAILING_SINCE: now.isoformat()})
        else:
            refused = now - datetime.fromisoformat(since) >= CLOSE_RETRY_FOR
        await db.commit()
        return refused, status
    except Exception:
        await db.rollback()
        logger.exception("account close: could not store a failure of %s", item_id)
    return False, status


def _without_since(data: dict) -> dict:
    return {k: v for k, v in data.items() if k != FAILING_SINCE}


async def _clear_since(db: AsyncSession, number: PhoneNumber) -> None:
    """After a release that worked: drop ``FAILING_SINCE``."""
    data = number.provisioning_metadata or {}
    if FAILING_SINCE in data:
        number.provisioning_metadata = _without_since(data)
        await db.commit()


def _unreleased(org_ids):
    """Numbers of the closed orgs a carrier may still hold: active, pending,
    or failed with a carrier number not given back yet, a number whose
    release was refused too."""
    return and_(
        PhoneNumber.organization_id.in_(org_ids),
        or_(
            PhoneNumber.provisioning_state.in_(("active", "pending")),
            PhoneNumber.provisioning_metadata.has_key("unreleased_resource_id"),
        ),
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
                    # unverified: a connected number whose check has not passed.
                    PhoneNumber.provisioning_state.in_(("active", "unverified")),
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
            if number.provider == "pbx":
                # Never a carrier call, never a stored refusal: a LiveKit
                # failure is retried on the next pass.
                if lk is None:
                    counts.failed += 1
                    logger.warning(
                        "account close: connected number %s (org %s) not "
                        "removed, LiveKit is not configured; retried next pass",
                        number_id,
                        org,
                    )
                    continue
                try:
                    await remove_connected_number(db, lk, number)
                except Exception as exc:
                    await db.rollback()
                    counts.failed += 1
                    logger.warning(
                        "account close: removal of connected number %s "
                        "(org %s) failed (%s); retried next pass",
                        number_id,
                        org,
                        type(exc).__name__,
                    )
                    continue
                counts.released += 1
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
            await _clear_since(db, number)
            counts.released += 1
        except Exception as exc:
            await db.rollback()
            refused, status = await _counts_as_refused(
                db, PhoneNumber, number_id, "provisioning_metadata", exc, now
            )
            if refused:
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


async def _delete_connections(
    db: AsyncSession, org_ids, lk: LiveKitClient | None, counts: CloseCounts
) -> None:
    """Delete each phone system connection of the closed orgs that has no
    live number left. A failure is retried on the next pass."""
    live_number = exists().where(
        PhoneNumber.sip_connection_id == SipConnection.id,
        PhoneNumber.provisioning_state.not_in(("released", "failed")),
    )
    rows = (
        await db.execute(
            select(SipConnection.id, SipConnection.organization_id).where(
                SipConnection.organization_id.in_(org_ids),
                SipConnection.deleted_at.is_(None),
                ~live_number,
            )
        )
    ).all()
    await db.commit()
    for conn_id, org in rows:
        try:
            if lk is None:
                raise RuntimeError("LiveKit is not configured")
            conn = await db.get(SipConnection, conn_id)
            if conn is None:
                continue
            await delete_connection(db, lk, conn)
        except Exception as exc:
            await db.rollback()
            counts.failed += 1
            logger.warning(
                "account close: delete of connection %s (org %s) failed "
                "(%s); retried next pass",
                conn_id,
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
            **_without_since(number.provisioning_metadata or {}),
            RELEASE_REFUSED: _refusal(status, now),
        }
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("account close: could not store refusal of %s", number_id)


async def _blocking_number(
    db: AsyncSession, row: CarrierVerification
) -> tuple[UUID, bool] | None:
    """``(number id, refused)`` for a number this registration covers that
    is not released, a number whose release was refused first (refused
    True), else None. A number verification covers the org's numbers of
    its carrier, country and type (older numbers do not name the
    verification they used); an SMS registration covers the numbers on it
    and its toll-free number. A toll-free number moved into the trial
    texting pool counts as refused: no workspace owns it, it is never
    released, and the cancel would delete its verification at the carrier,
    so the registration is kept for ops."""
    trial = await registration_trial_number(db, row)
    if trial is not None:
        return trial, True
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
    refused = PhoneNumber.provisioning_metadata.has_key(RELEASE_REFUSED)
    found = (
        await db.execute(
            select(PhoneNumber.id, refused)
            .where(live, tie)
            .order_by(refused.desc())
            .limit(1)
        )
    ).first()
    return None if found is None else (found[0], bool(found[1]))


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
            blocking = await _blocking_number(db, row)
            if blocking is not None and blocking[1]:
                # The carrier refused to release a number this registration
                # covers: the number still uses the registration, and its
                # delete by hand needs these ids. Marked for ops, kept.
                row.provider_refs = {
                    **_without_since(refs),
                    CLOSE_REFUSED: {
                        **_refusal(None, now),
                        "number_id": str(blocking[0]),
                    },
                }
                await db.commit()
                counts.refused += 1
                logger.warning(
                    "account close: verification %s kept, number %s it covers "
                    "was refused a release",
                    row.id,
                    blocking[0],
                )
                continue
            if blocking is not None:
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
                await counted_discard(
                    provider.discard({"brand_id": refs["kept_brand_id"]})
                )
                row.provider_refs = {
                    k: v
                    for k, v in refs.items()
                    if k not in ("kept_brand_id", FAILING_SINCE)
                }
                flag_modified(row, "provider_refs")
            else:
                await cancel_locked(db, row, provider, keep_brand=False, now=now)
            await db.commit()
            counts.cancelled += 1
        except Exception as exc:
            await db.rollback()
            refused, status = await _counts_as_refused(
                db, CarrierVerification, verification_id, "provider_refs", exc, now
            )
            if not refused:
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
                        **_without_since(row.provider_refs or {}),
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
    await _delete_connections(db, org_ids, lk, counts)
    await _cancel_registrations(db, org_ids, now, registry, counts)
    if any(counts.as_dict().values()):
        logger.info("account close: %s", counts.as_dict())
    return counts
