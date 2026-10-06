"""Set a number up for SMS at its carrier: attach it to the organization's
messaging service (Twilio) or messaging profile (Telnyx), creating that
once per organization. Hail runs this itself when an sms-capable number
becomes active and when a texts agent is assigned, so a customer never
has to; ``POST /numbers/{id}/enable-sms`` stays as the repair call.
"""

from __future__ import annotations

import logging
from typing import Literal

from hailhq.core.carrier_routing import sms_route
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.sms.base import SmsProvisioningError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class SmsSetupError(Exception):
    """``unavailable``: the carrier has no SMS route on this server (an
    operator problem). ``refused``: the carrier rejected the setup; the
    reason is logged, never shown."""

    def __init__(self, stage: Literal["unavailable", "refused"], detail: str) -> None:
        super().__init__(detail)
        self.stage = stage
        self.detail = detail


async def ensure_sms(db: AsyncSession, number: PhoneNumber) -> bool:
    """Attach ``number`` to its organization's messaging service. The caller
    holds the org lock and commits. Idempotent: an attached number is left
    alone. Returns True when the number was attached by this call."""
    if number.messaging_service_sid is not None:
        return False
    # One service per organization and carrier (a shared sender pool). Reuse
    # the one any sibling number already has; only when the org has none is
    # a fresh one created, so numbers never spawn orphan services.
    existing_sid = (
        await db.execute(
            select(PhoneNumber.messaging_service_sid)
            .where(
                PhoneNumber.organization_id == number.organization_id,
                PhoneNumber.messaging_service_sid.is_not(None),
                PhoneNumber.provider == number.provider,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    try:
        provider = sms_route(number.provider)
    except ValueError as exc:
        logger.error("%s SMS route unavailable: %s", number.provider, exc)
        raise SmsSetupError("unavailable", str(exc)) from exc
    try:
        sid = await provider.ensure_messaging_service(
            organization_id=number.organization_id, existing_sid=existing_sid
        )
        await provider.attach_number(
            messaging_service_sid=sid,
            provider_resource_id=number.provider_resource_id,
        )
    except Exception as exc:
        # Telnyx raises httpx / ValueError, Twilio SmsProvisioningError: all
        # of them mean "the carrier did not set the number up".
        detail = exc.detail if isinstance(exc, SmsProvisioningError) else str(exc)
        logger.error(
            "SMS setup refused for %s (%s, %s): %s",
            number.e164,
            number.provider,
            number.provider_resource_id,
            detail,
            exc_info=not isinstance(exc, SmsProvisioningError),
        )
        raise SmsSetupError("refused", detail) from exc
    number.messaging_service_sid = sid
    return True
