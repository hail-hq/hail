"""Org from-number resolution shared by the phone-channel create routes.

One query shape for both /calls and /sms: explicit ``from`` → that number,
iff org-owned + active (+ capability); otherwise the org's oldest active
number. Keeping it in one place stops the two routes drifting on number-
selection policy (provisioning states, capability checks, ordering).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import PhoneNumber

__all__ = ["resolve_org_number"]


async def resolve_org_number(
    db: AsyncSession,
    organization_id: UUID,
    explicit_e164: str | None,
    *,
    capability: str | None = None,
) -> PhoneNumber | None:
    """Resolve the org-owned sending number for an outbound send.

    ``capability`` (e.g. ``"sms"``) additionally requires the number's
    ``capabilities`` array to carry it — a number can legitimately be
    voice-only. Returns ``None`` when nothing matches; the caller owns the
    failure path (calls fall back to the shared pool, SMS 422s).
    """
    stmt = select(PhoneNumber).where(
        PhoneNumber.organization_id == organization_id,
        PhoneNumber.provisioning_state == "active",
    )
    if capability is not None:
        stmt = stmt.where(PhoneNumber.capabilities.any(capability))
    if explicit_e164 is not None:
        stmt = stmt.where(PhoneNumber.e164 == explicit_e164)
    else:
        stmt = stmt.order_by(PhoneNumber.created_at.asc()).limit(1)
    return (await db.execute(stmt)).scalar_one_or_none()
