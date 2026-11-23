"""US trial texting: a workspace can send up to 25 texts to its own verified
phone from a Hail-owned toll-free number. With no ``from`` the trial is
used only when the workspace's own number cannot text that US phone; a
``from`` equal to a ready trial number uses a trial text directly.

Trial numbers are pool numbers (``is_pool``, no organization) with
``pool_purpose = 'sms_trial'``; ``scripts/ops/sms_trial_numbers.py`` marks
them. A marked number is ready while ``provisioning_metadata`` holds
``APPROVED_AT_KEY``: ``refresh_trial_approvals`` (the verification worker's
pass) sets it when the carrier says its toll-free verification is approved
and removes it on a no. Requests read only the database."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import phonenumbers
from hailhq.core.carrier_routing import sms_route
from hailhq.core.compliance_gate import is_suppressed
from hailhq.core.db import key_lock, org_lock
from hailhq.core.models import CarrierVerification, PhoneNumber, Sms, SmsTrialPhone
from hailhq.core.sms_registration_gate import is_us
from sqlalchemy import DateTime, cast, func, literal_column, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SMS_TRIAL_LIMIT = 25
SMS_TRIAL_META_KEY = "sms_trial"
# ``phone_numbers.pool_purpose`` of a trial texting number.
POOL_PURPOSE = "sms_trial"
# ``provisioning_metadata`` key: when the carrier last said the number's
# toll-free verification is approved. Absent = not ready.
APPROVED_AT_KEY = "sms_trial_approved_at"
# A yes older than this no longer counts: lookups that keep failing (no
# answer) must not keep a number ready forever.
APPROVAL_MAX_AGE = timedelta(hours=24)
# ``provisioning_metadata`` key: an operator paused trial sending from the
# number (``scripts/ops/sms_trial_numbers.py pause``). Not ready while set.
PAUSED_KEY = "sms_trial_paused_at"
# Carriers whose SMS adapter answers ``tollfree_approved``. Any other
# carrier's number never becomes ready, so it cannot be marked.
TOLLFREE_APPROVAL_CARRIERS = frozenset({"twilio"})
TRIAL_PREFIX = "[Hail trial] "
TRIAL_FOOTER = "\nHail does not read replies."
TRIAL_HINT = (
    "To test now, verify your own mobile under Overview > Test a text, "
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


class PhoneStopped(Exception):
    def __init__(self) -> None:
        super().__init__("This phone replied STOP to Hail. Reply START first.")


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


def trial_body(body: str, *, limit: int, truncate: bool = False) -> str | None:
    """Prefix and footer around ``body``. None when it exceeds ``limit``;
    with ``truncate`` the body is cut so the footer stays."""
    room = limit - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)
    if len(body) > room:
        if not truncate:
            return None
        body = body[: max(room, 0)]
    return TRIAL_PREFIX + body + TRIAL_FOOTER


def _trial_numbers():
    """Active marked trial numbers that can text, oldest first. Pool rows
    only: ``organization_id IS NULL`` too, so a workspace never trial-texts
    from a number it owns (the pool CHECKs hold that in the database)."""
    return (
        select(PhoneNumber)
        .where(
            PhoneNumber.is_pool.is_(True),
            PhoneNumber.organization_id.is_(None),
            PhoneNumber.pool_purpose == POOL_PURPOSE,
            PhoneNumber.provisioning_state == "active",
            PhoneNumber.capabilities.any("sms"),
        )
        .order_by(PhoneNumber.created_at, PhoneNumber.id)
    )


def confirms_tollfree_approval(provider: str) -> bool:
    return provider in TOLLFREE_APPROVAL_CARRIERS


def _ready(stmt):
    """Only numbers whose carrier approval is cached as approved, with a yes
    from the last ``APPROVAL_MAX_AGE``, and not paused."""
    approved_at = cast(
        PhoneNumber.provisioning_metadata[APPROVED_AT_KEY].astext,
        DateTime(timezone=True),
    )
    return stmt.where(
        PhoneNumber.provisioning_metadata.has_key(APPROVED_AT_KEY),
        approved_at > func.now() - APPROVAL_MAX_AGE,
        ~PhoneNumber.provisioning_metadata.has_key(PAUSED_KEY),
    )


async def trial_configured(db: AsyncSession) -> bool:
    """At least one number is marked for trial texts."""
    return (await db.execute(_trial_numbers().limit(1))).first() is not None


async def ready_trial_number_ids(db: AsyncSession) -> list[UUID]:
    """Ids of the ready trial numbers, the sender first."""
    stmt = _ready(_trial_numbers()).with_only_columns(PhoneNumber.id)
    return list((await db.execute(stmt)).scalars().all())


async def trial_sender(db: AsyncSession) -> PhoneNumber | None:
    """The number that sends codes and trial texts: the oldest ready one.
    Database only, no carrier call. Adding a newer number keeps the sender;
    it changes only when this number stops being ready or is unmarked."""
    return (await db.execute(_ready(_trial_numbers()).limit(1))).scalar_one_or_none()


async def trial_number_by_e164(
    db: AsyncSession, e164: str, *, ready: bool = False
) -> PhoneNumber | None:
    """The marked trial number ``e164`` (ready only, with ``ready``)."""
    stmt = _trial_numbers().where(PhoneNumber.e164 == e164).limit(1)
    if ready:
        stmt = _ready(stmt)
    return (await db.execute(stmt)).scalar_one_or_none()


async def registration_trial_number(
    db: AsyncSession, registration: CarrierVerification
) -> UUID | None:
    """The live trial number a workspace's toll-free registration still
    covers (the number was moved into the trial pool with it). Its cancel
    would delete the number's toll-free verification at the carrier, so
    cancel and account close keep such a registration."""
    if registration.purpose != "sms" or registration.number_id is None:
        return None
    return (
        await db.execute(
            select(PhoneNumber.id).where(
                PhoneNumber.id == registration.number_id,
                PhoneNumber.pool_purpose == POOL_PURPOSE,
                PhoneNumber.provisioning_state.not_in(("released", "failed")),
            )
        )
    ).scalar_one_or_none()


async def registrations_kept_for_trial(
    db: AsyncSession, registrations: list[CarrierVerification]
) -> set[UUID]:
    """Ids of ``registrations`` that ``registration_trial_number`` keeps,
    in one query (for a list response's ``cancellable``)."""
    by_number = {
        r.number_id: r.id
        for r in registrations
        if r.purpose == "sms" and r.number_id is not None
    }
    if not by_number:
        return set()
    found = await db.execute(
        select(PhoneNumber.id).where(
            PhoneNumber.id.in_(by_number),
            PhoneNumber.pool_purpose == POOL_PURPOSE,
            PhoneNumber.provisioning_state.not_in(("released", "failed")),
        )
    )
    return {by_number[number_id] for number_id in found.scalars()}


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
    if await is_suppressed(db, organization_id, phone, "sms"):
        # STOP to a trial number opted it out for this workspace.
        raise PhoneStopped()
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
    """True when the trial can still send for this workspace: a trial number
    is ready and the workspace has trial texts left. Refusals add
    ``TRIAL_HINT`` only then; the caller checks that the destination is
    US."""
    if await trial_sender(db) is None:
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
    (the caller then answers as it does without the trial). It sends from
    ``trial_sender``, or from ``explicit_from`` when that is a ready trial
    number.

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
    # Cheapest checks first: the destination, then one indexed lookup, then
    # the trial number.
    if not is_us(to):
        return None
    if not await is_verified_trial_phone(db, organization_id, to):
        return None
    if explicit_from is not None:
        number = await trial_number_by_e164(db, explicit_from, ready=True)
    else:
        number = await trial_sender(db)
    if number is None or number.organization_id is not None:
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
    db: AsyncSession, organization_id: UUID, *, to: str, explicit_from: str
) -> str | None:
    """Why a text from the trial number ``explicit_from`` to ``to`` cannot
    go out, for a workspace whose ``claim_trial_send`` was refused."""
    if await trial_number_by_e164(db, explicit_from, ready=True) is None:
        return TRIAL_NOT_AVAILABLE
    if not is_us(to) or not await is_verified_trial_phone(db, organization_id, to):
        return TRIAL_WRONG_PHONE
    if await trial_texts_used(db, organization_id) >= SMS_TRIAL_LIMIT:
        return TRIAL_NONE_LEFT
    return None


async def _carrier_approval(provider: str, resource_id: str | None, e164: str):
    """The carrier's answer for one number: True, False, or None when there
    is no answer (carrier not configured here, lookup failed)."""
    if resource_id is None:
        return False
    try:
        route = sms_route(provider)
        # ``fresh``: the cached answer is what this pass refreshes.
        return await route.tollfree_approved(
            provider_resource_id=resource_id, e164=e164, fresh=True
        )
    except Exception:
        logger.warning("trial number approval lookup failed: %s", e164, exc_info=True)
        return None


async def refresh_trial_approvals(
    db: AsyncSession, *, number_ids: list[UUID] | None = None
) -> dict[str, int]:
    """Ask the carrier whether each marked trial number's toll-free
    verification is approved, and cache the answer on the row: a yes stamps
    ``APPROVED_AT_KEY``, a no removes it, no answer keeps the last one, so a
    slow carrier never turns the trial off (for ``APPROVAL_MAX_AGE``). Runs in the verification
    worker's pass and in the ops script, never on a request. No row lock
    is held during the carrier calls."""
    stmt = select(
        PhoneNumber.id,
        PhoneNumber.provider,
        PhoneNumber.provider_resource_id,
        PhoneNumber.e164,
        PhoneNumber.provisioning_metadata.has_key(APPROVED_AT_KEY).label("was"),
    ).where(
        PhoneNumber.is_pool.is_(True),
        PhoneNumber.pool_purpose == POOL_PURPOSE,
        PhoneNumber.provisioning_state == "active",
    )
    if number_ids is not None:
        stmt = stmt.where(PhoneNumber.id.in_(number_ids))
    rows = (await db.execute(stmt)).all()
    # End the read transaction before the carrier calls. The caller has no
    # pending changes (each row's answer is committed on its own below).
    await db.commit()
    # ``changed``: numbers whose answer flipped (yes to no or no to yes).
    counts = {"approved": 0, "not_approved": 0, "no_answer": 0, "changed": 0}
    for row in rows:
        answer = await _carrier_approval(
            row.provider, row.provider_resource_id, row.e164
        )
        if answer is None:
            counts["no_answer"] += 1
            continue
        if bool(answer) != row.was:
            counts["changed"] += 1
        if answer:
            value = func.jsonb_build_object(
                APPROVED_AT_KEY, datetime.now(UTC).isoformat()
            )
            new = PhoneNumber.provisioning_metadata.op("||")(value)
            counts["approved"] += 1
        else:
            new = PhoneNumber.provisioning_metadata.op("-")(APPROVED_AT_KEY)
            counts["not_approved"] += 1
        await db.execute(
            update(PhoneNumber)
            .where(PhoneNumber.id == row.id, PhoneNumber.pool_purpose == POOL_PURPOSE)
            .values(provisioning_metadata=new)
        )
        await db.commit()
    return counts
