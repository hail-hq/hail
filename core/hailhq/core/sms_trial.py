"""US trial texting: a workspace can send up to 25 texts to its own verified
phone from one Hail-owned toll-free number. With no ``from`` the trial is
used only when the workspace's own number cannot text that US phone; a
``from`` equal to the trial number uses a trial text directly."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import phonenumbers
from hailhq.core.config import settings
from hailhq.core.db import key_lock, org_lock
from hailhq.core.models import PhoneNumber, Sms, SmsTrialPhone
from hailhq.core.sms_registration_gate import is_us, on_approved_registration
from sqlalchemy import func, literal_column, select, text
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
# Why a text from the trial number cannot go out (``trial_refusal_reason``).
TRIAL_NOT_AVAILABLE = "Trial texting is not available."
TRIAL_WRONG_PHONE = "Trial texts go only to your verified trial phone."
TRIAL_NONE_LEFT = "No trial texts left."


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
    """The workspace asked for, or the phone got, the most codes allowed in
    ``CODE_WINDOW``. ``per_phone`` picks which one the copy names."""

    def __init__(self, retry_after: timedelta, *, per_phone: bool) -> None:
        if per_phone:
            msg = (
                f"This phone got the maximum of {MAX_CODE_SENDS_PER_DAY} codes "
                "in the last 24 hours. Try again later."
            )
        else:
            msg = (
                f"Your workspace asked for {MAX_CODE_SENDS_PER_DAY} codes in the "
                "last 24 hours. Try again later."
            )
        super().__init__(msg)
        self.retry_after = retry_after
        self.per_phone = per_phone


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


class TrialBodyTooLong(Exception):
    def __init__(self, limit: int) -> None:
        self.max_chars = limit - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)
        super().__init__(
            f"Trial texts can have up to {self.max_chars} characters. "
            "Nothing was sent."
        )


@dataclass(frozen=True)
class TrialSend:
    number: PhoneNumber
    body: str
    metadata: dict


@dataclass(frozen=True)
class IssuedCode:
    row: SmsTrialPhone
    code: str | None  # None: the phone is already verified, no text is sent
    phone: str  # canonical E.164: text the code here


def _code_phone(row: SmsTrialPhone) -> str:
    """The phone the open code was sent to."""
    return row.pending_phone_e164 or row.phone_e164


def code_text(code: str) -> str:
    return (
        f"Your Hail trial code is {code}. It expires in 10 minutes. "
        "Reply STOP to opt out, HELP for help."
    )


def _canonical_phone(phone: str) -> str:
    """``phone`` in canonical E.164, so every check, lock and stored value
    uses one spelling. Raises ``PhoneNotUs`` when it does not parse or has
    an impossible length."""
    try:
        parsed = phonenumbers.parse(phone, None)
    except phonenumbers.NumberParseException as exc:
        raise PhoneNotUs() from exc
    if not phonenumbers.is_possible_number(parsed):
        raise PhoneNotUs()
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


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
    return await trial_sender_of(db, await trial_number(db))


async def trial_sender_of(
    db: AsyncSession, number: PhoneNumber | None
) -> PhoneNumber | None:
    """``number`` (from ``trial_number``) when sending is on and it is on an
    approved registration, for a caller that already fetched it."""
    if not settings.hail_sms_trial_sending:
        return None
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
    """Reads the database, never a row already loaded in this session, so a
    check under ``org_lock`` sees a switch another transaction committed."""
    found = await db.execute(
        select(SmsTrialPhone.id)
        .where(
            SmsTrialPhone.organization_id == organization_id,
            SmsTrialPhone.status == "verified",
            SmsTrialPhone.phone_e164 == to,
        )
        .limit(1)
    )
    return found.first() is not None


async def start_phone_proof(
    db: AsyncSession,
    organization_id: UUID,
    *,
    phone: str,
    consent_version: str,
    user_id: UUID | None,
    now: datetime | None = None,
) -> IssuedCode:
    """Issue a code for ``phone``. The caller texts it to ``IssuedCode.phone``
    (the canonical form), then commits. With no code (the phone is already
    the verified one) the caller commits too: a waiting new phone is dropped.

    A verified row keeps its phone verified: the new phone waits in
    ``pending_phone_e164`` until ``confirm_phone``, so a typo never stops
    the trial texts."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    phone = _canonical_phone(phone)
    await org_lock(db, organization_id)
    if not is_us(phone):
        raise PhoneNotUs()
    is_hail = await db.execute(
        select(PhoneNumber.id)
        .where(
            PhoneNumber.e164 == phone,
            # A released number goes back to the carrier and can become a
            # person's mobile.
            PhoneNumber.provisioning_state.not_in(("released", "failed")),
        )
        .limit(1)
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
        if row.pending_phone_e164 is not None:
            # Asked again for the verified phone: drop the new phone that
            # waits and its code, so no stale pending phone shows.
            row.pending_phone_e164 = None
            row.code_hash = None
            row.code_expires_at = None
            row.code_attempts = 0
            row.updated_at = now
            await db.flush()
        return IssuedCode(row=row, code=None, phone=phone)

    cutoff = now - CODE_WINDOW
    log = [
        e
        for e in (row.code_send_log if row is not None else [])
        if datetime.fromisoformat(e["at"]) > cutoff
    ]
    if len(log) >= MAX_CODE_SENDS_PER_DAY:
        oldest = min(datetime.fromisoformat(e["at"]) for e in log)
        raise CodeBudgetExceeded(oldest + CODE_WINDOW - now, per_phone=False)

    # Per-phone cap across all workspaces, counting entries left by a
    # workspace that later switched phone. The lock makes the count and the
    # send log write atomic per phone (always after org_lock).
    await key_lock(db, f"sms_trial_phone:{phone}")
    # The containment test uses the GIN index to find the rows that hold
    # this phone; only their entries are unpacked and filtered.
    at_rows = (
        await db.execute(
            text(
                "SELECT (e->>'at')::timestamptz AS at FROM sms_trial_phones, "
                "jsonb_array_elements(code_send_log) e "
                "WHERE code_send_log @> CAST(:probe AS jsonb) "
                "AND e->>'phone' = :phone AND (e->>'at')::timestamptz > :cutoff"
            ),
            {
                "probe": json.dumps([{"phone": phone}]),
                "phone": phone,
                "cutoff": cutoff,
            },
        )
    ).all()
    if len(at_rows) >= MAX_CODE_SENDS_PER_DAY:
        raise CodeBudgetExceeded(
            min(r.at for r in at_rows) + CODE_WINDOW - now, per_phone=True
        )

    code = f"{secrets.randbelow(10**6):06d}"
    if row is not None and row.status == "verified":
        # Keep the verified phone and its status until the new one confirms.
        target = {"pending_phone_e164": phone}
    else:
        target = {
            "phone_e164": phone,
            "pending_phone_e164": None,
            "status": "pending",
            "verified_at": None,
        }
    fields = {
        **target,
        "code_hash": _hash_code(organization_id, phone, code),
        "code_expires_at": now + CODE_TTL,
        "code_attempts": 0,
        "code_send_log": [
            *log,
            {"phone": phone, "at": now.astimezone(UTC).isoformat()},
        ],
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
    return IssuedCode(row=row, code=code, phone=phone)


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
    expected = _hash_code(organization_id, _code_phone(row), code.strip())
    if not hmac.compare_digest(expected, row.code_hash):
        row.code_attempts += 1
        row.updated_at = now
        tries_left = max(MAX_CODE_TRIES - row.code_attempts, 0)
        if tries_left == 0:
            row.code_hash = None
            row.code_expires_at = None
            row.pending_phone_e164 = None
        await db.flush()
        raise WrongCode(tries_left)
    try:
        async with db.begin_nested():
            # The unique index on verified phones maps a phone another
            # workspace verified meanwhile to PhoneTaken.
            row.phone_e164 = _code_phone(row)
            row.pending_phone_e164 = None
            row.status = "verified"
            row.verified_at = now
            row.code_hash = None
            row.code_expires_at = None
            row.code_attempts = 0
            row.updated_at = now
            await db.flush()
    except IntegrityError as exc:
        raise PhoneTaken() from exc
    return row


async def trial_texts_used(db: AsyncSession, organization_id: UUID) -> int:
    """Trial texts this workspace sent. A failed text never reached the
    phone and is not billed, so it does not count."""
    return (
        await db.execute(
            select(func.count())
            .select_from(Sms)
            .where(
                Sms.organization_id == organization_id,
                Sms.direction == "outbound",
                # The literal key, as in the partial index ``sms_trial_org_idx``:
                # a bound key cannot match its predicate in a generic plan.
                Sms.metadata_.op("?")(literal_column(f"'{SMS_TRIAL_META_KEY}'")),
                Sms.status != "failed",
            )
        )
    ).scalar_one()


async def trial_hint_applies(db: AsyncSession, organization_id: UUID) -> bool:
    """True when the trial can still send for this workspace: sending is on,
    the workspace is not the trial number's owner, and it has trial texts
    left. Refusals add ``TRIAL_HINT`` only then; the caller checks that the
    destination is US."""
    number = await trial_sender(db)
    if number is None or number.organization_id == organization_id:
        return False
    return await trial_texts_used(db, organization_id) < SMS_TRIAL_LIMIT


async def claim_trial_send(
    db: AsyncSession,
    organization_id: UUID,
    *,
    to: str,
    body: str,
    limit: int,
    explicit_from: str | None = None,
    truncate: bool = False,
) -> TrialSend | None:
    """The trial send for this text, or None when the trial does not apply
    (the caller then answers as it does without the trial).

    Raises ``TrialBodyTooLong`` when the wrapped body exceeds ``limit`` and
    ``truncate`` is off.

    Caller contract: on a ``TrialSend``, this transaction holds the
    workspace's ``org_lock``. Insert the ``Sms`` row with
    ``TrialSend.metadata`` merged last (so no caller metadata can drop the
    trial key) and commit in this same transaction, with no commit in
    between. The commit releases the lock, so the next claim counts this
    text and the cap of ``SMS_TRIAL_LIMIT`` holds under concurrent sends.
    The verified phone is checked under the lock too, so a text never goes
    to a phone the workspace switched away from.
    """
    # Cheapest checks first: settings, then one indexed lookup, then the
    # trial number and its registration.
    if not trial_enabled():
        return None
    if explicit_from is not None and explicit_from != settings.hail_sms_trial_number:
        return None
    if not is_us(to):
        return None
    if not await is_verified_trial_phone(db, organization_id, to):
        return None
    number = await trial_sender(db)
    if number is None:
        return None
    if organization_id == number.organization_id:
        return None
    await org_lock(db, organization_id)
    # Again under the lock: start_phone_proof and confirm_phone take the same
    # lock, so the phone cannot change between this check and the commit.
    # The check before the lock keeps other sends from waiting on it.
    if not await is_verified_trial_phone(db, organization_id, to):
        return None
    if await trial_texts_used(db, organization_id) >= SMS_TRIAL_LIMIT:
        return None
    wrapped = trial_body(body, limit=limit, truncate=truncate)
    if wrapped is None:
        raise TrialBodyTooLong(limit)
    return TrialSend(number=number, body=wrapped, metadata={SMS_TRIAL_META_KEY: True})


async def trial_refusal_reason(
    db: AsyncSession, organization_id: UUID, *, to: str
) -> str | None:
    """Why a text from the trial number to ``to`` cannot go out, for a
    workspace whose ``claim_trial_send`` was refused. None for the trial
    number's owner, which sends from it as its own number."""
    number = await trial_number(db)
    if number is not None and number.organization_id == organization_id:
        return None
    if await trial_sender_of(db, number) is None:
        return TRIAL_NOT_AVAILABLE
    if not is_us(to) or not await is_verified_trial_phone(db, organization_id, to):
        return TRIAL_WRONG_PHONE
    if await trial_texts_used(db, organization_id) >= SMS_TRIAL_LIMIT:
        return TRIAL_NONE_LEFT
    return None
