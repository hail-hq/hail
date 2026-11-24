"""Connection service for connected numbers: connections and their numbers.

A connection owns two LiveKit trunks (inbound always, outbound when the PBX
host is known). LiveKit calls come first and the row second, so a failure
after a trunk exists removes the trunks it made and leaves no half-made row.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Final, Literal
from uuid import UUID, uuid4

import phonenumbers
from hailhq.core.billing import CALL_META_BILLED, has_funds
from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.carrier_routing import voice_route
from hailhq.core.compliance_gate import check_handover_allowed
from hailhq.core.db import org_lock, session_scope
from hailhq.core.internal_webhook import notify_usage_event_recorded
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import Call, CallEvent, PhoneNumber, SipConnection, UsageEvent
from hailhq.core.pool import (
    CALL_META_FROM_POOL,
    claim_pool_number,
    release_pool_reservation,
)
from hailhq.core.schemas import TERMINAL_CALL_STATUSES
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.sip_connections import (
    CHECK_CALL_META,
    CHECK_CODE_DIGITS,
    CHECK_CODE_META,
    CHECK_CODE_SECONDS,
    CHECK_RING_SECONDS,
    CHECKS_PER_DAY,
    UNVERIFIED_TTL,
    check_in_flight,
    check_outcomes,
    decrypt_secret,
    encrypt_secret,
    hash_check_code,
    new_inbound_username,
    new_password,
    number_shape,
)
from hailhq.core.telephony_catalog import sells_in
from livekit.api import SipCallError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

UNSET: Final = object()

_TRANSPORTS = ("auto", "udp", "tcp", "tls")
_HOST = re.compile(
    r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*"
)
_NUMERIC = re.compile(r"[0-9.]+")
_BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".svc",
    ".cluster.local",
    ".home.arpa",
)


class ConnectedNumberError(Exception):
    """A connected-number request the service refuses.

    ``code`` is one of: invalid_input, number_taken, invalid_number,
    has_numbers, connection_deleted, not_unverified, check_in_progress,
    check_limit, no_funds, not_callable, pool_exhausted, livekit_failed,
    host_not_allowed, password_required, not_configured, not_found.
    """

    def __init__(self, code: str, field: str | None = None, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.field = field


def _invalid(field: str) -> ConnectedNumberError:
    return ConnectedNumberError("invalid_input", field, f"invalid {field}")


def _clean_label(label: Any) -> str:
    if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
        raise _invalid("label")
    return label.strip()


def _check_region(region: Any) -> str:
    if region not in phonenumbers.SUPPORTED_REGIONS:
        raise _invalid("default_region")
    return region


def _check_transport(transport: Any) -> str:
    if transport not in _TRANSPORTS:
        raise _invalid("pbx_transport")
    return transport


def _check_port(port: Any) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise _invalid("pbx_port")
    return port


def _check_host(host: Any) -> str | None:
    if host is None:
        return None
    if not isinstance(host, str) or len(host) > 253 or not _HOST.fullmatch(host):
        raise _invalid("pbx_host")
    # No DNS lookup: refuse names and addresses that point inside a network.
    if _NUMERIC.fullmatch(host):
        try:
            allowed = ipaddress.ip_address(host).is_global
        except ValueError:
            allowed = False
    else:
        name = host.lower()
        allowed = (
            "." in name
            and all(len(label) <= 63 for label in name.split("."))
            and not any(
                name == suffix.lstrip(".") or name.endswith(suffix)
                for suffix in _BLOCKED_SUFFIXES
            )
        )
    if not allowed:
        raise ConnectedNumberError(
            "host_not_allowed", "pbx_host", "that host is not allowed"
        )
    return host


async def _livekit(call, *args):
    try:
        return await call(*args)
    except Exception as exc:
        logger.warning("LiveKit trunk call failed: %s", exc)
        raise ConnectedNumberError(
            "livekit_failed", None, "the phone system could not be reached"
        ) from exc


async def _drop_trunks(lk: LiveKitClient, trunk_ids: list[str]) -> None:
    """Best effort: delete trunks made by a call that then failed."""
    for trunk_id in trunk_ids:
        try:
            await lk.delete_sip_trunk(trunk_id)
        except Exception:
            logger.warning("could not delete orphan trunk %s", trunk_id, exc_info=True)


async def create_connection(
    db: AsyncSession,
    lk: LiveKitClient,
    *,
    organization_id: UUID,
    label: str,
    default_region: str,
    pbx_host: str | None = None,
    pbx_port: int = 5060,
    pbx_transport: str = "auto",
    pbx_username: str | None = None,
    pbx_password: str | None = None,
) -> tuple[SipConnection, str]:
    """Create a connection and its trunks. Returns the row and the inbound
    password in plain text (shown once)."""
    label = _clean_label(label)
    _check_region(default_region)
    pbx_host = _check_host(pbx_host)
    _check_port(pbx_port)
    _check_transport(pbx_transport)
    if pbx_password is not None and pbx_username is None:
        raise _invalid("pbx_password")

    # Raises SecretKeyMissing before any LiveKit call.
    encrypted_pbx_password = (
        encrypt_secret(pbx_password) if pbx_password is not None else None
    )
    connection_id = uuid4()
    username = new_inbound_username()
    password = new_password()
    encrypted_inbound = encrypt_secret(password)

    created: list[str] = []
    try:
        inbound_id = await _livekit(
            lk.create_pbx_inbound_trunk, connection_id, username, password
        )
        created.append(inbound_id)
        outbound_id = None
        if pbx_host is not None:
            outbound_id = await _livekit(
                lk.create_pbx_outbound_trunk,
                connection_id,
                pbx_host,
                pbx_port,
                pbx_transport,
                pbx_username,
                pbx_password,
            )
            created.append(outbound_id)
        row = SipConnection(
            id=connection_id,
            organization_id=organization_id,
            label=label,
            inbound_username=username,
            encrypted_inbound_password=encrypted_inbound,
            pbx_host=pbx_host,
            pbx_port=pbx_port,
            pbx_transport=pbx_transport,
            pbx_username=pbx_username,
            encrypted_pbx_password=encrypted_pbx_password,
            default_region=default_region,
            livekit_inbound_trunk_id=inbound_id,
            livekit_outbound_trunk_id=outbound_id,
        )
        db.add(row)
        await db.flush()
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        except Exception:
            logger.warning("rollback failed after connection create", exc_info=True)
        await _drop_trunks(lk, created)
        raise
    return row, password


def _live(connection: SipConnection) -> None:
    if connection.deleted_at is not None:
        raise ConnectedNumberError("connection_deleted", None, "connection deleted")


async def update_connection(
    db: AsyncSession,
    lk: LiveKitClient,
    connection: SipConnection,
    *,
    label: Any = UNSET,
    pbx_host: Any = UNSET,
    pbx_port: Any = UNSET,
    pbx_transport: Any = UNSET,
    pbx_username: Any = UNSET,
    pbx_password: Any = UNSET,
) -> SipConnection:
    """Change a connection; the outbound trunk follows ``pbx_host``."""
    _live(connection)
    new_label = connection.label if label is UNSET else _clean_label(label)
    host = connection.pbx_host if pbx_host is UNSET else _check_host(pbx_host)
    port = connection.pbx_port if pbx_port is UNSET else _check_port(pbx_port)
    transport = (
        connection.pbx_transport
        if pbx_transport is UNSET
        else _check_transport(pbx_transport)
    )
    username = connection.pbx_username if pbx_username is UNSET else pbx_username
    if username is not None and not isinstance(username, str):
        raise _invalid("pbx_username")

    if pbx_password is UNSET:
        # Dropping the username drops its password.
        encrypted_password = connection.encrypted_pbx_password if username else None
    elif pbx_password is None:
        encrypted_password = None
    else:
        if not isinstance(pbx_password, str) or username is None:
            raise _invalid("pbx_password")
        encrypted_password = encrypt_secret(pbx_password)
    password = decrypt_secret(encrypted_password) if encrypted_password else None

    destination_changed = (host, port, transport) != (
        connection.pbx_host,
        connection.pbx_port,
        connection.pbx_transport,
    )
    if (
        host is not None
        and destination_changed
        and connection.encrypted_pbx_password is not None
        and pbx_password is UNSET
    ):
        # Never send a stored password to a new destination.
        raise ConnectedNumberError(
            "password_required", "pbx_password", "send the PBX password again"
        )

    old_host = connection.pbx_host
    old_outbound = connection.livekit_outbound_trunk_id
    outbound_id = old_outbound
    created: str | None = None
    drop: str | None = None
    if host is None:
        if old_outbound is not None:
            drop = old_outbound
            outbound_id = None
    elif old_outbound is None:
        created = outbound_id = await _livekit(
            lk.create_pbx_outbound_trunk,
            connection.id,
            host,
            port,
            transport,
            username,
            password,
        )
    elif (host, port, transport, username, encrypted_password) != (
        old_host,
        connection.pbx_port,
        connection.pbx_transport,
        connection.pbx_username,
        connection.encrypted_pbx_password,
    ):
        await _livekit(
            lk.update_pbx_outbound_trunk,
            old_outbound,
            host,
            port,
            transport,
            username,
            password,
        )

    try:
        connection.label = new_label
        connection.pbx_host = host
        connection.pbx_port = port
        connection.pbx_transport = transport
        connection.pbx_username = username
        connection.encrypted_pbx_password = encrypted_password
        connection.livekit_outbound_trunk_id = outbound_id
        await db.commit()
    except BaseException:
        await db.rollback()
        if created is not None:
            await _drop_trunks(lk, [created])
        raise
    if drop is not None:
        await _drop_trunks(lk, [drop])
    return connection


async def rotate_inbound_password(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection
) -> str:
    """Give the inbound trunk a new password. Returns it in plain text."""
    _live(connection)
    password = new_password()
    encrypted = encrypt_secret(password)
    old_password = decrypt_secret(connection.encrypted_inbound_password)
    trunk_id = connection.livekit_inbound_trunk_id
    # Read before the commit: a rollback expires the row.
    username = connection.inbound_username
    await _livekit(lk.set_pbx_inbound_credentials, trunk_id, username, password)
    try:
        connection.encrypted_inbound_password = encrypted
        await db.commit()
    except BaseException:
        await db.rollback()
        try:
            await lk.set_pbx_inbound_credentials(trunk_id, username, old_password)
        except Exception:
            logger.warning("could not restore inbound password", exc_info=True)
        raise
    return password


async def delete_connection(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection
) -> None:
    """Delete both trunks and tombstone the row (numbers still point at it)."""
    await org_lock(db, connection.organization_id)
    await db.refresh(connection)
    _live(connection)
    live = await db.scalar(
        select(func.count())
        .select_from(PhoneNumber)
        .where(
            PhoneNumber.sip_connection_id == connection.id,
            PhoneNumber.provisioning_state.not_in(("released", "failed")),
        )
    )
    if live:
        await db.rollback()
        raise ConnectedNumberError("has_numbers", None, "numbers are still connected")
    try:
        for trunk_id in (
            connection.livekit_inbound_trunk_id,
            connection.livekit_outbound_trunk_id,
        ):
            if trunk_id is not None:
                await _livekit(lk.delete_sip_trunk, trunk_id)
        connection.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def add_connected_number(
    db: AsyncSession, lk: LiveKitClient, connection: SipConnection, e164: str
) -> PhoneNumber:
    """Put ``e164`` on the connection's inbound trunk and insert it as
    ``unverified``. The org lock keeps this from racing ``delete_connection``."""
    try:
        country, number_type = number_shape(e164)
    except ValueError:
        country = None
    # Non-geographic numbers (+800...) have region "001", not an ISO country.
    if country is None or country == "001":
        raise ConnectedNumberError("invalid_number", "e164", "not a valid number")
    await org_lock(db, connection.organization_id)
    await db.refresh(connection)
    try:
        _live(connection)
    except ConnectedNumberError:
        await db.rollback()
        raise
    trunk_id = connection.livekit_inbound_trunk_id
    number = PhoneNumber(
        organization_id=connection.organization_id,
        e164=e164,
        country_code=country,
        number_type=number_type,
        capabilities=["voice"],
        is_pool=False,
        provider="pbx",
        provider_resource_id=None,
        sip_connection_id=connection.id,
        provisioning_state="unverified",
    )
    db.add(number)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if "phone_numbers_e164_live_uniq" not in str(exc):
            raise
        raise ConnectedNumberError(
            "number_taken", "e164", "that number is already in use"
        ) from exc
    try:
        await _livekit(lk.add_inbound_number, trunk_id, e164)
    except ConnectedNumberError:
        await db.rollback()
        raise
    try:
        await db.commit()
    except BaseException:
        await db.rollback()
        try:
            await lk.remove_inbound_number(trunk_id, e164)
        except Exception:
            logger.warning(
                "could not take %s off the trunk after a failed commit",
                e164,
                exc_info=True,
            )
        raise
    return number


async def remove_connected_number(
    db: AsyncSession,
    lk: LiveKitClient,
    number: PhoneNumber,
    *,
    only_state: str | None = None,
) -> PhoneNumber:
    """Take a connected number off its trunk and tombstone the row.
    Idempotent. The trunk keeps its placeholder entry. With ``only_state``,
    a row in another state (read under the lock) is left as it is."""
    await org_lock(db, number.organization_id)
    await db.refresh(number)
    if only_state is not None and number.provisioning_state != only_state:
        await db.commit()  # nothing written; ends the transaction and the lock
        return number
    if number.provisioning_state == "released":
        await db.rollback()
        return number
    connection = await db.get(SipConnection, number.sip_connection_id)
    trunk_id = connection.livekit_inbound_trunk_id
    e164 = number.e164
    try:
        await _livekit(lk.remove_inbound_number, trunk_id, e164)
    except ConnectedNumberError:
        await db.rollback()
        raise
    number.provisioning_state = "released"
    number.released_at = datetime.now(timezone.utc)
    number.voice_agent_id = None
    number.sms_agent_id = None
    number.inbound_registered_at = None
    try:
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return number


async def remove_stale_unverified_numbers(
    lk: LiveKitClient | None, *, now: datetime | None = None
) -> int:
    """Remove ``unverified`` numbers older than ``UNVERIFIED_TTL``. Each one
    in its own transaction; a failure is logged and the rest go on."""
    if lk is None:
        logger.debug("stale connected numbers not removed: LiveKit is not configured")
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - UNVERIFIED_TTL
    async with session_scope() as db:
        ids = (
            (
                await db.execute(
                    select(PhoneNumber.id)
                    .where(
                        PhoneNumber.provider == "pbx",
                        PhoneNumber.provisioning_state == "unverified",
                        PhoneNumber.created_at < cutoff,
                    )
                    .order_by(PhoneNumber.created_at)
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
    removed = 0
    for number_id in ids:
        try:
            async with session_scope() as db:
                number = await db.get(PhoneNumber, number_id)
                if number is None or number.provisioning_state != "unverified":
                    continue
                # The ownership check may activate it before the lock.
                number = await remove_connected_number(
                    db, lk, number, only_state="unverified"
                )
                if number.provisioning_state == "released":
                    removed += 1
        except Exception:
            logger.warning(
                "could not remove stale connected number %s", number_id, exc_info=True
            )
    return removed


# --------------------------------------------------------------------------- #
# Ownership check, outbound side
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CheckResult:
    state: Literal["active", "unverified"]
    reason: Literal["no_call_arrived", "not_authenticated", "code_not_received"] | None
    call_id: UUID | None


# After a pass, how long the outbound leg stays up so the line can play.
CHECK_LINE_SECONDS = 6
# After the dial returns, how long to wait for the inbound side's result.
CHECK_RESULT_WAIT_SECONDS = CHECK_CODE_SECONDS + 3
# The check Call's max_duration_seconds, same as CHECK_WINDOW.
CHECK_MAX_DURATION_SECONDS = 90
CHECK_POLL_SECONDS = 0.25
_CHECK_FINAL = ("passed", "code_not_received", "not_authenticated")
# Hail-side failures before the dial: not a check the customer used up.
_CHECK_SETUP_FAILURES = (
    CallEndReason.CARRIER_ROUTE_FAILED.value,
    CallEndReason.ROOM_CREATE_FAILED.value,
    CallEndReason.AGENT_DISPATCH_FAILED.value,
)


def _check_identity(call_id: UUID) -> str:
    return f"pbx-check-{call_id}"


async def _checks_today(
    db: AsyncSession, organization_id: UUID, e164: str, now: datetime
) -> int:
    """Check calls to ``e164`` in this organization in the last 24 h, by
    number, not by row id: removing and adding the number again gives it a
    new id but not a new allowance. A Hail-side failure before the dial does
    not count; one in flight (no end reason yet) does."""
    return await db.scalar(
        select(func.count())
        .select_from(Call)
        .where(
            Call.organization_id == organization_id,
            Call.to_e164 == e164,
            Call.direction == "outbound",
            Call.metadata_.has_key(CHECK_CALL_META),
            Call.requested_at > now - timedelta(hours=24),
            or_(
                Call.end_reason.is_(None),
                Call.end_reason.not_in(_CHECK_SETUP_FAILURES),
            ),
        )
    )


async def _refuse_check(db: AsyncSession, code: str) -> ConnectedNumberError:
    # Nothing written: a commit ends the lock and keeps loaded rows readable
    # (a rollback would expire them).
    await db.commit()
    return ConnectedNumberError(code, None, code)


async def check_connected_number(
    db: AsyncSession,
    lk: LiveKitClient,
    number: PhoneNumber,
    *,
    now: datetime | None = None,
) -> CheckResult:
    """Prove the customer's phone system owns ``number``: call it from a pool
    line and send a 6-digit code as key presses. The inbound side
    (``hailhq.core.inbound_calls.finish_check``) turns the number ``active``
    when the call arrives on the connection's trunk with the right code.

    Billed like any outbound call from a pool number, only when answered. No
    customer webhook. The code is in the voicebot's dispatch only, never in
    the database (a keyed hash is) or in a log line."""
    now = now or datetime.now(timezone.utc)
    code = f"{secrets.randbelow(10**CHECK_CODE_DIGITS):0{CHECK_CODE_DIGITS}d}"
    try:
        code_hash = hash_check_code(code)
    except SecretKeyMissing as exc:
        raise ConnectedNumberError(
            "not_configured", None, "connected numbers are not configured"
        ) from exc

    # 1. Under the org lock: the state, then every refusal, then the Call.
    await org_lock(db, number.organization_id)
    await db.refresh(number)
    number_id = number.id
    org_id = number.organization_id
    e164 = number.e164
    if number.provisioning_state == "active" and number.provider == "pbx":
        await db.commit()  # nothing written; ends the transaction and the lock
        return CheckResult("active", None, None)
    if number.provider != "pbx" or number.provisioning_state != "unverified":
        raise await _refuse_check(db, "not_unverified")
    # 2. One check at a time, CHECKS_PER_DAY a day.
    if await check_in_flight(db, number_id, now) is not None:
        raise await _refuse_check(db, "check_in_progress")
    if await _checks_today(db, org_id, e164, now) >= CHECKS_PER_DAY:
        raise await _refuse_check(db, "check_limit")
    # 3. A billed call to a destination Hail calls.
    if not await has_funds(db, org_id):
        raise await _refuse_check(db, "no_funds")
    if not sells_in(number.country_code):
        raise await _refuse_check(db, "not_callable")
    if not (await check_handover_allowed(db, org_id, e164)).allowed:
        raise await _refuse_check(db, "not_callable")
    # 4. A pool line.
    pool = await claim_pool_number(db, capability="voice")
    if pool is None:
        raise await _refuse_check(db, "pool_exhausted")
    # 5. The check Call, set by Hail only (POST /calls refuses pbx_check_ keys).
    call = Call(
        organization_id=org_id,
        from_number_id=pool.id,
        from_e164=pool.e164,
        to_e164=e164,
        provider=pool.provider,
        direction="outbound",
        status="ringing",
        voice_config={},
        max_duration_seconds=CHECK_MAX_DURATION_SECONDS,
        metadata_={
            CALL_META_BILLED: True,
            CALL_META_FROM_POOL: True,
            CHECK_CALL_META: str(number_id),
            CHECK_CODE_META: code_hash,
        },
    )
    db.add(call)
    await db.flush()
    call_id = call.id
    pool.reserved_call_id = call_id
    await db.commit()  # drops the org lock before the dial
    logger.info("ownership check call %s for number %s placed", call_id, number_id)

    # 6-7. Dial, then wait for the inbound side's result.
    room_name: str | None = None
    started_at: datetime | None = None
    answered_at: datetime | None = None
    failure: str | None = None
    stage = "carrier_route"
    try:
        trunk_id, headers = await voice_route(db, pool)
        await db.commit()  # no transaction open while it rings
        stage = "room_create"
        room_name = await lk.create_room(call_id)
        stage = "agent_dispatch"
        await lk.dispatch_agent(
            room_name=room_name,
            agent_name="hail-voicebot",
            metadata={"call_id": str(call_id), "pbx_check_send": code},
        )
        stage = "sip_participant"
        started_at = datetime.now(timezone.utc)
        try:
            await lk.create_sip_participant(
                room_name=room_name,
                to_e164=e164,
                from_e164=pool.e164,
                sip_trunk_id=trunk_id,
                participant_identity=_check_identity(call_id),
                wait_until_answered=True,
                ringing_timeout_seconds=CHECK_RING_SECONDS,
                **({"headers": headers} if headers else {}),
            )
        except SipCallError as exc:
            logger.info(
                "ownership check call %s not answered (sip %s)",
                call_id,
                exc.sip_status_code,
            )
        else:
            answered_at = datetime.now(timezone.utc)
            stage = "result_wait"  # log label only: answered calls never fail
            loop = asyncio.get_running_loop()
            deadline = loop.time() + CHECK_RESULT_WAIT_SECONDS
            outcomes: set[str] = set()
            while True:
                outcomes = await check_outcomes(db, call_id)
                await db.commit()  # ends the read; the next one sees new events
                if outcomes & set(_CHECK_FINAL) or loop.time() >= deadline:
                    break
                await asyncio.sleep(CHECK_POLL_SECONDS)
            if "passed" in outcomes:
                await asyncio.sleep(CHECK_LINE_SECONDS)
    except Exception as exc:
        # The type only: the dispatch request that failed holds the code.
        logger.warning(
            "ownership check call %s failed at %s (%s)",
            call_id,
            stage,
            type(exc).__name__,
        )
        if answered_at is None:
            failure = CallEndReason(f"{stage}_failed").value
    finally:
        try:
            # A failed or cancelled statement leaves the transaction broken;
            # the finalize needs a clean one. Only reads are open here.
            await db.rollback()
        except Exception:
            logger.warning("rollback failed for check call %s", call_id, exc_info=True)
        if room_name is not None:
            try:
                await lk.delete_room(room_name)
            except Exception:
                logger.warning(
                    "could not delete ownership check room %s",
                    room_name,
                    exc_info=True,
                )
        # 8. Finalize, also when cancelled: the pool line must come back.
        await _finish_check_call(
            db, call_id, org_id, room_name, started_at, answered_at, failure
        )

    if failure in _CHECK_SETUP_FAILURES:
        raise ConnectedNumberError(
            "livekit_failed", None, "the check call could not be placed"
        )

    # 9. The result, read after the finalize: a pass that lands late counts.
    await db.refresh(number)
    if number.provisioning_state == "active":
        return CheckResult("active", None, call_id)
    outcomes = await check_outcomes(db, call_id)
    await db.commit()
    if "passed" in outcomes:
        return CheckResult("active", None, call_id)
    if "not_authenticated" in outcomes:
        return CheckResult("unverified", "not_authenticated", call_id)
    if outcomes & {"arrived", "code_not_received"}:
        return CheckResult("unverified", "code_not_received", call_id)
    return CheckResult("unverified", "no_call_arrived", call_id)


async def _finish_check_call(
    db: AsyncSession,
    call_id: UUID,
    organization_id: UUID,
    room_name: str | None,
    started_at: datetime | None,
    answered_at: datetime | None,
    failure: str | None,
) -> None:
    """Close the check call once (a call another writer closed stays as it
    is and is not billed), free the pool line, bill the answered time."""
    ended_at = datetime.now(timezone.utc)
    if failure is not None and answered_at is None:
        status, end_reason = "failed", failure
    elif answered_at is not None:
        status, end_reason = "completed", CallEndReason.NORMAL_HANGUP.value
    else:
        status, end_reason = "no_answer", CallEndReason.USER_UNAVAILABLE.value
    usage_event_id: str | None = None
    try:
        result = await db.execute(
            update(Call)
            .where(Call.id == call_id, Call.status.not_in(TERMINAL_CALL_STATUSES))
            .values(
                status=status,
                end_reason=end_reason,
                livekit_room=room_name,
                started_at=started_at,
                answered_at=answered_at,
                ended_at=ended_at,
            )
        )
        if (result.rowcount or 0) > 0:
            db.add(
                CallEvent(
                    call_id=call_id,
                    kind="state_change",
                    payload={"from": "ringing", "to": status, "reason": end_reason},
                )
            )
            if answered_at is not None:
                usage = UsageEvent(
                    organization_id=organization_id,
                    channel="voice",
                    units=max(int((ended_at - answered_at).total_seconds() * 1000), 0),
                    ref=str(call_id),
                )
                db.add(usage)
                await db.flush()
                usage_event_id = str(usage.id)
        await release_pool_reservation(db, call_id=call_id)
        await db.commit()
    except BaseException:
        logger.warning(
            "could not finalize ownership check call %s", call_id, exc_info=True
        )
        await db.rollback()
        raise
    if usage_event_id is not None:
        notify_usage_event_recorded(usage_event_id)
