"""Helpers for connected numbers: a customer's PBX on a SIP connection.

Must not import ``carrier_routing``: it imports this module.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from uuid import UUID

import phonenumbers
from hailhq.core.config import settings
from hailhq.core.models import Call, CallEvent, PhoneNumber, SipConnection
from hailhq.core.provider_config import provider_cipher
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.core.secret_cipher import SecretKeyMissing
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

CHECK_RING_SECONDS = 30
# How long the inbound leg waits for the 6-digit code.
CHECK_CODE_SECONDS = 15
CHECKS_PER_DAY = 5
# How long the voicebot may take to say the line after a pass.
PBX_CHECK_LINE_SECONDS = 10
# A check call newer than this, not terminal, is in flight.
# Ring 30 + result wait 18 + line 12 = 60 s, under 90.
CHECK_WINDOW = timedelta(seconds=90)
UNVERIFIED_TTL = timedelta(days=7)
# calls.metadata key on an ownership check call. Every check key starts with
# ``PBX_CHECK_METADATA_PREFIX``, which POST /calls rejects.
CHECK_CALL_META = "pbx_check_number_id"
# call_events.kind written by the inbound side.
CHECK_EVENT_KIND = "pbx_check"
# calls.metadata key on the check call: a keyed hash of the code (see
# ``hash_check_code``), never the code.
CHECK_CODE_META = "pbx_check_code_sha256"
CHECK_CODE_DIGITS = 6

_TYPES = {
    phonenumbers.PhoneNumberType.FIXED_LINE: "local",
    phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "local",
    phonenumbers.PhoneNumberType.MOBILE: "mobile",
    phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
}


def _ascii_digits(s: str) -> bool:
    """``str.isdigit()`` is also true for Arabic-Indic and other digits."""
    return s.isascii() and s.isdigit()


def carrier_caller(raw: str) -> str:
    """``442079460000`` -> ``+442079460000``. Carriers differ on the leading
    ``+`` (Telnyx and Twilio send it, DIDWW may not); ``phone_numbers.e164``
    always has it. Anything that is not all digits stays as given."""
    raw = (raw or "").strip()
    if raw and raw[0] != "+" and _ascii_digits(raw):
        return "+" + raw
    return raw


def normalize_caller(raw: str, default_region: str) -> str:
    """A PBX caller as E.164 where possible. An extension (1 to 6 digits) and
    anything that is not a number (``anonymous``) stay as given."""
    raw = (raw or "").strip()
    digits = raw.removeprefix("+")
    if not _ascii_digits(digits):
        return raw
    if not raw.startswith("+") and len(digits) <= 6:
        return raw
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return raw
    if not phonenumbers.is_valid_number(parsed):
        return raw
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def new_inbound_username() -> str:
    return "hail-" + secrets.token_hex(6)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def number_shape(e164: str) -> tuple[str, str]:
    """(country_code, number_type) of ``e164``; ``ValueError`` if invalid."""
    try:
        parsed = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"not a valid number: {e164}") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError(f"not a valid number: {e164}")
    region = phonenumbers.region_code_for_number(parsed)
    if not region:
        raise ValueError(f"not a valid number: {e164}")
    kind = phonenumbers.number_type(parsed)
    return region, _TYPES.get(kind, "national")


def encrypt_secret(plain: str) -> str:
    return provider_cipher().encrypt(plain)


def decrypt_secret(token: str) -> str:
    return provider_cipher().decrypt(token)


async def connection_of(db: AsyncSession, number: PhoneNumber) -> SipConnection:
    """The live connection of a ``pbx`` number; ``ValueError`` otherwise."""
    if number.sip_connection_id is None:
        raise ValueError("number has no SIP connection")
    conn = await db.get(SipConnection, number.sip_connection_id)
    if conn is None or conn.deleted_at is not None:
        raise ValueError("SIP connection is missing or deleted")
    return conn


# Separates the check code key from every other use of the secret key.
_CHECK_CODE_KEY_LABEL = b"hail connected number check code v1"


def _check_code_key() -> bytes:
    secret = settings.hail_provider_secret_key
    if not secret:
        raise SecretKeyMissing(
            "HAIL_PROVIDER_SECRET_KEY must be set to check a connected number"
        )
    return hmac.new(secret.encode(), _CHECK_CODE_KEY_LABEL, hashlib.sha256).digest()


def hash_check_code(code: str) -> str:
    """HMAC-SHA256 hex of an ownership check code, keyed from
    ``HAIL_PROVIDER_SECRET_KEY``: a plain hash of 6 digits is reversed by
    trying all 1,000,000 codes. Raises ``SecretKeyMissing`` without the key."""
    return hmac.new(_check_code_key(), code.encode(), hashlib.sha256).hexdigest()


def check_code_matches(digits: str, code_sha256: object) -> bool:
    """True only when ``digits`` is exactly the code: the first
    ``CHECK_CODE_DIGITS`` key presses, no search over a longer string.
    Without the secret key it refuses (False): the check never passes."""
    if len(digits) != CHECK_CODE_DIGITS or not isinstance(code_sha256, str):
        return False
    try:
        expected = hash_check_code(digits)
    except SecretKeyMissing:
        return False
    return hmac.compare_digest(expected.encode(), code_sha256.encode())


def _check_calls(number_id: UUID):
    """Calls that are ownership checks of ``number_id``: placed by Hail from
    a pool number, outbound, to that number, in the number's organization,
    and marked with its id. A customer can place a call from a pool number,
    but cannot set the ``pbx_check_`` keys (POST /calls rejects them)."""
    target = aliased(PhoneNumber)
    pool = aliased(PhoneNumber)
    return (
        select(Call)
        .join(
            target,
            and_(
                target.id == number_id,
                Call.to_e164 == target.e164,
                Call.organization_id == target.organization_id,
            ),
        )
        .join(pool, and_(pool.id == Call.from_number_id, pool.is_pool.is_(True)))
        .where(
            Call.direction == "outbound",
            Call.metadata_[CHECK_CALL_META].astext == str(number_id),
        )
    )


async def check_in_flight(
    db: AsyncSession, number_id: UUID, now: datetime
) -> Call | None:
    """The newest ownership check call of ``number_id`` that is not finished
    and started within ``CHECK_WINDOW``. The one rule both sides use: the
    inbound side accepts a check only then, the outbound side refuses a
    second check while one runs."""
    return (
        await db.execute(
            _check_calls(number_id)
            .where(
                Call.status.not_in(TERMINAL_CALL_STATUSES),
                Call.requested_at >= now - CHECK_WINDOW,
            )
            .order_by(Call.requested_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def check_call(db: AsyncSession, call_id: UUID, number_id: UUID) -> Call | None:
    """Call ``call_id`` if it is an ownership check call of ``number_id``
    (finished or not), else None."""
    return (
        await db.execute(_check_calls(number_id).where(Call.id == call_id))
    ).scalar_one_or_none()


async def check_outcomes(db: AsyncSession, call_id: UUID) -> set[str]:
    """The outcomes the inbound side wrote for check call ``call_id``
    (``CHECK_EVENT_KIND`` events): ``arrived``, ``not_authenticated``,
    ``passed``, ``code_not_received``."""
    rows = await db.execute(
        select(CallEvent.payload["outcome"].astext).where(
            CallEvent.call_id == call_id, CallEvent.kind == CHECK_EVENT_KIND
        )
    )
    return {outcome for outcome in rows.scalars() if outcome}
