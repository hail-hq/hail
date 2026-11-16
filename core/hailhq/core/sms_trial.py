"""US trial texting: a workspace without a US registration can text its own
verified phone from one Hail-owned toll-free number."""

from __future__ import annotations

from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from hailhq.core.sms_registration_gate import on_approved_registration
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

SMS_TRIAL_LIMIT = 25
SMS_TRIAL_META_KEY = "sms_trial"
TRIAL_PREFIX = "[Hail trial] "
TRIAL_FOOTER = "\nHail does not read replies."
TRIAL_HINT = (
    "To test now, verify your own mobile under Numbers > Trial texting, "
    "then send to it without from."
)


def trial_enabled() -> bool:
    return bool(settings.hail_sms_trial_number) and settings.hail_sms_trial_sending


def trial_body(body: str, *, limit: int, truncate: bool = False) -> str | None:
    """Prefix and footer around ``body``. None when it exceeds ``limit``;
    with ``truncate`` the body is cut so the footer stays."""
    room = limit - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)
    if len(body) > room:
        if not truncate:
            return None
        body = body[: max(room, 0)]
    return TRIAL_PREFIX + body + TRIAL_FOOTER


async def trial_number(db: AsyncSession) -> PhoneNumber | None:
    """The configured trial number: active and able to text."""
    if not settings.hail_sms_trial_number:
        return None
    return (
        await db.execute(
            select(PhoneNumber)
            .where(
                PhoneNumber.e164 == settings.hail_sms_trial_number,
                PhoneNumber.provisioning_state == "active",
                PhoneNumber.capabilities.any("sms"),
            )
            .limit(1)
        )
    ).scalar_one_or_none()


async def trial_sender(db: AsyncSession) -> PhoneNumber | None:
    """The trial number when sending is on and it is on an approved
    registration. Database only, no carrier call."""
    if not settings.hail_sms_trial_sending:
        return None
    number = await trial_number(db)
    if number is None or number.organization_id is None:
        return None
    if not await on_approved_registration(db, number.organization_id, number):
        return None
    return number
