"""US trial texting: a workspace without a US registration can text its own
verified phone from one Hail-owned toll-free number."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import org_lock
from hailhq.core.models import PhoneNumber, SmsTrialPhone
from hailhq.core.sms_registration_gate import is_us, on_approved_registration
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

SMS_TRIAL_LIMIT = 25
SMS_TRIAL_META_KEY = "sms_trial"
TRIAL_PREFIX = "[Hail trial] "
TRIAL_FOOTER = "\nHail does not read replies."
TRIAL_HINT = (
    "To test now, verify your own mobile under Numbers > Trial texting, "
    "then send to it without from."
)
CODE_TTL = timedelta(minutes=10)
MAX_CODE_TRIES = 5
MAX_CODE_SENDS_PER_DAY = 5
CODE_WINDOW = timedelta(hours=24)


class PhoneNotUs(Exception):
    def __init__(self) -> None:
        super().__init__("Trial texts go to US mobile numbers only.")


class PhoneIsHailNumber(Exception):
    def __init__(self) -> None:
        super().__init__("Use your own mobile phone, not a Hail number.")


class PhoneTaken(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Another workspace uses this phone for trial texts. Use a different phone."
        )


class CodeBudgetExceeded(Exception):
    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(
            f"You asked for {MAX_CODE_SENDS_PER_DAY} codes today. Try again tomorrow."
        )
        self.retry_after = retry_after


class NoPendingCode(Exception):
    def __init__(self) -> None:
        super().__init__("Send a code first.")


class CodeExpired(Exception):
    def __init__(self) -> None:
        super().__init__("The code expired. Send a new code.")


class WrongCode(Exception):
    def __init__(self, tries_left: int) -> None:
        if tries_left > 0:
            super().__init__(f"The code is not correct. {tries_left} tries left.")
        else:
            super().__init__("The code is not correct. Send a new code.")
        self.tries_left = tries_left


@dataclass(frozen=True)
class IssuedCode:
    row: SmsTrialPhone
    code: str | None  # None: the phone is already verified, no text is sent


def code_text(code: str) -> str:
    return (
        f"Your Hail trial code is {code}. It expires in 10 minutes. "
        "Reply STOP to opt out, HELP for help."
    )


def _hash_code(organization_id: UUID, phone: str, code: str) -> str:
    return hashlib.sha256(f"{organization_id}:{phone}:{code}".encode()).hexdigest()


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


async def get_trial_phone(
    db: AsyncSession, organization_id: UUID
) -> SmsTrialPhone | None:
    return (
        await db.execute(
            select(SmsTrialPhone).where(
                SmsTrialPhone.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()


async def is_verified_trial_phone(
    db: AsyncSession, organization_id: UUID, to: str
) -> bool:
    row = await get_trial_phone(db, organization_id)
    return row is not None and row.status == "verified" and row.phone_e164 == to


async def start_phone_proof(
    db: AsyncSession,
    organization_id: UUID,
    *,
    phone: str,
    consent_version: str,
    user_id: UUID | None,
    now: datetime | None = None,
) -> IssuedCode:
    """Issue a code for ``phone``. The caller texts it, then commits."""
    now = now or datetime.now(UTC)
    await org_lock(db, organization_id)
    if not is_us(phone):
        raise PhoneNotUs()
    is_hail = await db.execute(
        select(PhoneNumber.id).where(PhoneNumber.e164 == phone).limit(1)
    )
    if is_hail.first() is not None:
        raise PhoneIsHailNumber()
    taken = await db.execute(
        select(SmsTrialPhone.id)
        .where(
            SmsTrialPhone.phone_e164 == phone,
            SmsTrialPhone.status == "verified",
            SmsTrialPhone.organization_id != organization_id,
        )
        .limit(1)
    )
    if taken.first() is not None:
        raise PhoneTaken()

    row = await get_trial_phone(db, organization_id)
    if row is not None and row.status == "verified" and row.phone_e164 == phone:
        return IssuedCode(row=row, code=None)

    sends, window_start = 0, now
    if row is not None and row.code_window_started_at is not None:
        if now - row.code_window_started_at < CODE_WINDOW:
            sends, window_start = row.code_sends, row.code_window_started_at
    if sends >= MAX_CODE_SENDS_PER_DAY:
        raise CodeBudgetExceeded(window_start + CODE_WINDOW - now)

    code = f"{secrets.randbelow(10**6):06d}"
    fields = {
        "phone_e164": phone,
        "status": "pending",
        "verified_at": None,
        "code_hash": _hash_code(organization_id, phone, code),
        "code_expires_at": now + CODE_TTL,
        "code_attempts": 0,
        "code_sends": sends + 1,
        "code_window_started_at": window_start,
        "consent_version": consent_version,
        "consent_at": now,
        "consent_user_id": user_id,
        "updated_at": now,
    }
    if row is None:
        row = SmsTrialPhone(organization_id=organization_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    await db.flush()
    return IssuedCode(row=row, code=code)


async def confirm_phone(
    db: AsyncSession,
    organization_id: UUID,
    code: str,
    *,
    now: datetime | None = None,
) -> SmsTrialPhone:
    """Check ``code``. A wrong try is counted with a flush: the caller must
    commit that count before it reports the error."""
    now = now or datetime.now(UTC)
    await org_lock(db, organization_id)
    row = await get_trial_phone(db, organization_id)
    if row is None or row.code_hash is None or row.code_expires_at is None:
        raise NoPendingCode()
    if now > row.code_expires_at:
        raise CodeExpired()
    expected = _hash_code(organization_id, row.phone_e164, code.strip())
    if not hmac.compare_digest(expected, row.code_hash):
        row.code_attempts += 1
        tries_left = max(MAX_CODE_TRIES - row.code_attempts, 0)
        if tries_left == 0:
            row.code_hash = None
            row.code_expires_at = None
        await db.flush()
        raise WrongCode(tries_left)
    row.status = "verified"
    row.verified_at = now
    row.code_hash = None
    row.code_expires_at = None
    row.code_attempts = 0
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise PhoneTaken() from exc
    return row
