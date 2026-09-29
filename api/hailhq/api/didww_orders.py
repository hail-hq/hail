"""DIDWW-only steps of a number order. DIDWW registers the end user after
the purchase, can reject days later, and keeps the setup fee; the other
carriers have none of this. ``number_orders.py`` calls in here and keeps
the money writes (``finish_order``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hailhq.api.audit import write_audit_log
from hailhq.core.carrier_routing import DIDWW
from hailhq.core.db import org_lock, session_scope
from hailhq.core.models import CarrierVerification, PhoneNumber
from hailhq.core.providers.voice.didww import (
    release_didww_number,
    revoke_registration,
    terminate_did,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def stop_renewal(number: PhoneNumber, did_id: str) -> None:
    """Terminate a DIDWW number the order will not keep. Never raises. When
    the carrier call fails, the DID id is kept on the row as
    ``unterminated_did_id``: a failed order stores no resource id, and the
    DID renews at the carrier until ``retry_unterminated_dids`` gets it
    terminated. Saved by the caller's ``finish_order``."""
    try:
        await terminate_did(did_id)
    except Exception:
        logger.exception(
            "Could not terminate DIDWW number; will retry: number=%s did=%s",
            number.id,
            did_id,
        )
        number.provisioning_metadata = {
            **number.provisioning_metadata,
            "unterminated_did_id": did_id,
        }


async def rejected_registration(
    db: AsyncSession, number: PhoneNumber, did_id: str | None
) -> str:
    """DIDWW refused the end-user registration of a bought number. Stops
    the DID's renewal, takes the approval back and marks the Hail
    verification rejected. Returns the reason shown to the buyer.

    Caller holds the org lock and fails the order. The carrier calls run
    under that lock on purpose: two concurrent reconciles must never both
    see "pending" and both submit a terminate for the same DID."""
    org = number.organization_id
    if did_id:
        await stop_renewal(number, did_id)
    # Take the approval back, so the next quote asks for new papers
    # instead of filing the same rejected ones again.
    reason = None
    address_id = number.provisioning_metadata.get("offer", {}).get("address_id")
    if address_id:
        try:
            reason = await revoke_registration(
                address_id, org, number.country_code, number.number_type
            )
        except Exception:
            logger.exception(
                "Could not revoke rejected DIDWW registration; re-stamp the "
                "address by hand: number=%s address=%s",
                number.id,
                address_id,
            )
    reason = reason or "the carrier rejected the end-user registration"
    verification = (
        await db.execute(
            select(CarrierVerification).where(
                CarrierVerification.organization_id == org,
                CarrierVerification.provider == DIDWW,
                CarrierVerification.country_code == number.country_code,
                CarrierVerification.number_type == number.number_type,
                CarrierVerification.state == "approved",
            )
        )
    ).scalar_one_or_none()
    if verification is not None:
        verification.state = "rejected"
        verification.rejection_reason = reason
        verification.updated_at = datetime.now(timezone.utc)
        # Same audit trail as every other system-driven rejection
        # (see _reject_row in routes/verifications.py).
        await write_audit_log(
            org,
            None,
            "verification.reject",
            "carrier_verification",
            verification.id,
            {"rejected_by": None, "reason": reason},
            actor_user_id=None,
            actor_kind="system",
        )
    return reason


async def retry_terminate(db: AsyncSession, number: PhoneNumber) -> None:
    """Terminate a DID whose terminate failed when its order was failed.
    Carrier errors propagate and the id stays for the next try. The carrier
    call runs under the org lock, like the first terminate, so two runs
    never act on the same row."""
    await org_lock(db, number.organization_id)
    await db.refresh(number)
    did_id = number.provisioning_metadata.get("unterminated_did_id")
    if did_id:
        # Tolerates a DID that is already gone at the carrier.
        await release_didww_number(did_id)
        number.provisioning_metadata = {
            k: v
            for k, v in number.provisioning_metadata.items()
            if k != "unterminated_did_id"
        }
    await db.commit()


async def retry_unterminated_dids():
    """Fresh session per number, like ``reconcile_pending_orders``. Oldest
    ``updated_at`` first; a row that failed again goes to the back."""
    async with session_scope() as db:
        ids = (
            (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provider == DIDWW,
                        PhoneNumber.provisioning_metadata.has_key(
                            "unterminated_did_id"
                        ),
                    )
                    .order_by(PhoneNumber.updated_at)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    for number_id in ids:
        try:
            async with session_scope() as db:
                number = await db.get(PhoneNumber, number_id)
                if number:
                    await retry_terminate(db, number)
        except Exception:
            logger.warning(
                "DIDWW terminate retry failed: number=%s; will retry",
                number_id,
                exc_info=True,
            )
            # Move the row to the back, so rows beyond the limit get a turn.
            async with session_scope() as db:
                await db.execute(
                    update(PhoneNumber)
                    .where(PhoneNumber.id == number_id)
                    .values(updated_at=datetime.now(timezone.utc))
                )
                await db.commit()
