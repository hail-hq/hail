"""Connected phone systems: a customer's PBX sends calls to Hail agents.

POST   /sip-connections                                   create (returns the password)
GET    /sip-connections                                   list, newest first
GET    /sip-connections/{id}                              one connection
PATCH  /sip-connections/{id}                              label, PBX address, credentials
POST   /sip-connections/{id}/rotate-password              new password
GET    /sip-connections/{id}/password                     the current password (audited)
DELETE /sip-connections/{id}                              delete (no numbers left)
POST   /sip-connections/{id}/numbers                      add a number (unverified)
POST   /sip-connections/{id}/numbers/{number_id}/verify   ownership check call
DELETE /sip-connections/{id}/numbers/{number_id}          remove a number from Hail

Thin routes over ``hailhq.api.connected_numbers``. Passwords appear only in
the create, rotate and password responses: no idempotency store, no audit
payload, and ``Cache-Control: no-store``.
"""

from __future__ import annotations

from typing import Annotated, Final
from uuid import UUID

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.connected_numbers import (
    ConnectedNumberError,
    add_connected_number,
    check_connected_number,
    create_connection,
    delete_connection,
    remove_connected_number,
    rotate_inbound_password,
    update_connection,
)
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.api.routes.calls import get_livekit_optional
from hailhq.core.config import settings
from hailhq.core.db import get_session, independent_session_scope
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import AuditLog, PhoneNumber, SipConnection
from hailhq.core.schemas import (
    ConnectedNumberCheck,
    ConnectedNumberCreate,
    PhoneNumberResponse,
    SipConnectionCreate,
    SipConnectionCreated,
    SipConnectionListResponse,
    SipConnectionPassword,
    SipConnectionPatch,
    SipConnectionResponse,
)
from hailhq.core.secret_cipher import SecretKeyMissing
from hailhq.core.sip_connections import decrypt_secret
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/sip-connections",
    tags=["sip-connections"],
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)

# Connected phone systems are open to every workspace, API keys included.
# False makes them staff-only again: only a staff console session (a JWT the
# website minted with superadmin) reaches these routes, everyone else gets
# 404. Change it together with hail-website's `connectedPhoneSystems` feature.
OPEN_TO_ALL_WORKSPACES: Final = True

# Characters of the SIP server a workspace sees; Hail's team sends the full
# address (see _sees_full_host).
MASKED_HOST_CHARS: Final = 5


def _sees_full_host(principal: Principal) -> bool:
    """Hail staff (a staff console session) and a self-hosted operator (the
    shared key) see the full SIP server; every workspace sees its start."""
    staff = principal.auth_kind == "jwt" and principal.superadmin
    return staff or principal.auth_kind == "shared"


def _masked_host(host: str) -> str:
    return host[:MASKED_HOST_CHARS] + "…" if len(host) > MASKED_HOST_CHARS else host


async def connections_principal(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """The caller, when connected phone systems are open to it."""
    staff = principal.auth_kind == "jwt" and principal.superadmin
    if not OPEN_TO_ALL_WORKSPACES and not staff:
        raise HTTPException(status_code=404, detail="Not Found")
    return principal


_DEFAULT_LIST_LIMIT = 50
_MAX_LIST_LIMIT = 100

# Every ConnectedNumberError code: (HTTP status, customer copy).
_ERRORS: dict[str, tuple[int, str]] = {
    "invalid_input": (422, "This value is not valid."),
    "invalid_number": (
        422,
        "Enter the number with its country code, like +14155550100.",
    ),
    "host_not_allowed": (
        422,
        (
            "Use your phone system's public address. Private and local "
            "addresses are not allowed."
        ),
    ),
    "password_required": (
        422,
        (
            "Send the phone system password again when you change its "
            "address, port or transport."
        ),
    ),
    "not_callable": (422, "Hail cannot call this number to check it."),
    "number_taken": (409, "This number is already on Hail."),
    "has_numbers": (409, "Remove this connection's numbers first."),
    "connection_deleted": (409, "This connection was deleted."),
    "not_unverified": (409, "This number is already checked."),
    "check_in_progress": (
        409,
        "This number is being checked. Try again in 2 minutes.",
    ),
    "too_many_unverified": (
        409,
        "Check or remove the numbers waiting for their check first.",
    ),
    "check_limit": (429, "This number used all its checks for today. Try tomorrow."),
    "no_funds": (402, "Add credit to run the check. It is a normal outbound call."),
    "not_found": (404, "Not found."),
    "pool_exhausted": (
        503,
        "Hail has no free line to run the check now. Try again in a few minutes.",
    ),
    "livekit_failed": (502, "Hail could not reach its call service. Try again."),
    "not_configured": (503, "Connected phone systems are not set up on this server."),
}

_ERROR_RESPONSES: dict = {
    402: {"description": "Not enough credit for the check call."},
    404: {"description": "The connection or number does not exist here."},
    409: {"description": "The request conflicts with the current state."},
    422: {"description": "A field is not valid."},
    429: {"description": "The number used all its checks for today."},
    502: {"description": "Hail could not reach its call service."},
    503: {"description": "Connected phone systems are not set up on this server."},
}


def _http_error(exc: ConnectedNumberError) -> HTTPException:
    status, message = _ERRORS.get(exc.code, (500, "Something went wrong."))
    if status == 422:
        return unprocessable(message, loc=["body", exc.field] if exc.field else None)
    return HTTPException(status_code=status, detail=message)


def _not_configured() -> HTTPException:
    return _http_error(ConnectedNumberError("not_configured"))


def _livekit_or_503(lk: LiveKitClient | None) -> LiveKitClient:
    if lk is None or not settings.livekit_sip_host:
        raise _not_configured()
    return lk


async def _connection_or_404(
    db: AsyncSession, organization_id: UUID, connection_id: UUID
) -> SipConnection:
    row = await db.scalar(
        select(SipConnection).where(
            SipConnection.id == connection_id,
            SipConnection.organization_id == organization_id,
            SipConnection.deleted_at.is_(None),
        )
    )
    if row is None:
        raise _http_error(ConnectedNumberError("not_found"))
    return row


async def _number_or_404(
    db: AsyncSession, connection: SipConnection, number_id: UUID
) -> PhoneNumber:
    row = await db.scalar(
        select(PhoneNumber).where(
            PhoneNumber.id == number_id,
            PhoneNumber.organization_id == connection.organization_id,
            PhoneNumber.sip_connection_id == connection.id,
            PhoneNumber.provider == "pbx",
        )
    )
    if row is None:
        raise _http_error(ConnectedNumberError("not_found"))
    return row


def _response(row: SipConnection, principal: Principal) -> SipConnectionResponse:
    try:
        last4 = decrypt_secret(row.encrypted_inbound_password)[-4:]
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    except (InvalidToken, TypeError, AttributeError):
        # A rotated key or a damaged value: the connection still lists;
        # rotate-password makes a new one.
        last4 = ""
    full_host = _sees_full_host(principal)
    return SipConnectionResponse(
        id=row.id,
        label=row.label,
        default_region=row.default_region,
        sip_host=(
            settings.livekit_sip_host
            if full_host
            else _masked_host(settings.livekit_sip_host)
        ),
        sip_host_masked=not full_host,
        username=row.inbound_username,
        password_last4=last4,
        pbx_host=row.pbx_host,
        pbx_port=row.pbx_port,
        pbx_transport=row.pbx_transport,
        pbx_username=row.pbx_username,
        pbx_password_set=row.encrypted_pbx_password is not None,
        created_at=row.created_at,
    )


async def _audit(
    principal: Principal, action: str, resource_type: str, resource_id: UUID, payload
) -> None:
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        payload=payload,
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )


async def record_password_read(principal: Principal, connection_id: UUID) -> None:
    """The audit row of a password read, committed before the password is
    returned. Raises on failure (``write_audit_log`` never does)."""
    actor_user_id, actor_kind = actor_of(principal)
    async with independent_session_scope() as session:
        session.add(
            AuditLog(
                organization_id=principal.organization_id,
                api_key_id=principal.api_key_id,
                action="sip_connection.read_password",
                resource_type="sip_connection",
                resource_id=connection_id,
                payload={},
                actor_user_id=actor_user_id,
                actor_kind=actor_kind,
            )
        )
        await session.commit()


@router.post(
    "",
    response_model=SipConnectionCreated,
    status_code=http_status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_sip_connection(
    body: SipConnectionCreate,
    response: Response,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> SipConnectionCreated:
    """Connect your phone system (PBX). Add the returned SIP server, username
    and password to it as a generic SIP trunk. Read the password again with
    GET /sip-connections/{id}/password, or rotate it to get a new one. With ``pbx_host``, agents can
    also call out and hand calls over through your phone system."""
    lk = _livekit_or_503(lk)
    try:
        row, password = await create_connection(
            db,
            lk,
            organization_id=principal.organization_id,
            label=body.label,
            default_region=body.default_region,
            pbx_host=body.pbx_host,
            pbx_port=body.pbx_port,
            pbx_transport=body.pbx_transport,
            pbx_username=body.pbx_username,
            pbx_password=body.pbx_password,
        )
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    await _audit(
        principal,
        "sip_connection.create",
        "sip_connection",
        row.id,
        {"label": row.label},
    )
    response.headers["Cache-Control"] = "no-store"
    return SipConnectionCreated(
        **_response(row, principal).model_dump(), password=password
    )


@router.get("", response_model=SipConnectionListResponse)
async def list_sip_connections(
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
) -> SipConnectionListResponse:
    """List your connected phone systems, newest first. Passwords show only
    their last 4 characters."""
    stmt = select(SipConnection).where(
        SipConnection.organization_id == principal.organization_id,
        SipConnection.deleted_at.is_(None),
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        SipConnection.created_at,
        SipConnection.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return SipConnectionListResponse(
        items=[_response(r, principal) for r in rows], next_cursor=next_cursor
    )


@router.get(
    "/{connection_id}", response_model=SipConnectionResponse, responses=_ERROR_RESPONSES
)
async def get_sip_connection(
    connection_id: UUID,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SipConnectionResponse:
    """Read one connected phone system."""
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    return _response(row, principal)


@router.patch(
    "/{connection_id}", response_model=SipConnectionResponse, responses=_ERROR_RESPONSES
)
async def update_sip_connection(
    connection_id: UUID,
    body: SipConnectionPatch,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> SipConnectionResponse:
    """Change the label, or where and how Hail dials your phone system.
    Fields left out stay as they are. ``pbx_host: null`` stops calls out and
    handover through it; calls in keep working."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    try:
        row = await update_connection(db, lk, row, **fields)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    await _audit(
        principal,
        "sip_connection.update",
        "sip_connection",
        row.id,
        {"fields": sorted(fields)},
    )
    return _response(row, principal)


@router.post(
    "/{connection_id}/rotate-password",
    response_model=SipConnectionPassword,
    responses=_ERROR_RESPONSES,
)
async def rotate_sip_connection_password(
    connection_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> SipConnectionPassword:
    """Make a new password for your phone system to send. The old one stops
    working at once: put the new one in your phone system's trunk."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    try:
        password = await rotate_inbound_password(db, lk, row)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    await _audit(
        principal, "sip_connection.rotate_password", "sip_connection", row.id, {}
    )
    response.headers["Cache-Control"] = "no-store"
    return SipConnectionPassword(password=password)


@router.get(
    "/{connection_id}/password",
    response_model=SipConnectionPassword,
    responses={
        404: _ERROR_RESPONSES[404],
        409: {"description": "This password cannot be read. Make a new one."},
        503: {
            "description": (
                "Connected phone systems are not set up on this server, or "
                "the read could not be recorded. No password is returned."
            )
        },
    },
)
async def read_sip_connection_password(
    connection_id: UUID,
    response: Response,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SipConnectionPassword:
    """The password your phone system sends to Hail, to copy into its trunk
    again. Every read is recorded in the workspace's audit log."""
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    try:
        password = decrypt_secret(row.encrypted_inbound_password)
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    except (InvalidToken, TypeError, AttributeError) as exc:
        raise HTTPException(
            status_code=409,
            detail="This password cannot be read. Make a new one.",
        ) from exc
    try:
        await record_password_read(principal, row.id)
    except Exception as exc:
        # Every read is recorded: no record, no password.
        raise HTTPException(
            status_code=503, detail="The password could not be read. Try again."
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    return SipConnectionPassword(password=password)


@router.delete(
    "/{connection_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=_ERROR_RESPONSES,
)
async def delete_sip_connection(
    connection_id: UUID,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> None:
    """Delete a connected phone system. Remove its numbers first."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    row_id, label = row.id, row.label
    try:
        await delete_connection(db, lk, row)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    await _audit(
        principal, "sip_connection.delete", "sip_connection", row_id, {"label": label}
    )


@router.post(
    "/{connection_id}/numbers",
    response_model=PhoneNumberResponse,
    status_code=http_status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def add_sip_connection_number(
    connection_id: UUID,
    body: ConnectedNumberCreate,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> PhoneNumberResponse:
    """Add a number your phone system holds. It starts ``unverified``: send
    its incoming calls to Hail, then run the ownership check (verify) soon:
    until it passes, someone else can buy or connect the number, and Hail
    removes numbers left unchecked."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    try:
        number = await add_connected_number(db, lk, row, body.e164)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    await _audit(
        principal, "number.connect", "phone_number", number.id, {"e164": number.e164}
    )
    return PhoneNumberResponse.model_validate(number)


@router.post(
    "/{connection_id}/numbers/{number_id}/verify",
    response_model=ConnectedNumberCheck,
    responses=_ERROR_RESPONSES,
)
async def verify_sip_connection_number(
    connection_id: UUID,
    number_id: UUID,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> ConnectedNumberCheck:
    """Prove you own the number: Hail calls it over the phone network, and
    the call must come back through this connection with a code sent as key
    presses. Takes up to 2 minutes. Billed as a normal outbound call. A
    failed check is a 200 with ``state: unverified`` and a ``reason``."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    number = await _number_or_404(db, row, number_id)
    if number.provisioning_state == "released":
        # Removed by the customer, after 7 days, or because another
        # workspace bought or connected it.
        raise HTTPException(
            status_code=409, detail="This number was removed from Hail. Add it again."
        )
    try:
        result = await check_connected_number(db, lk, number)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    return ConnectedNumberCheck(
        state=result.state, reason=result.reason, call_id=result.call_id
    )


@router.delete(
    "/{connection_id}/numbers/{number_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=_ERROR_RESPONSES,
)
async def remove_sip_connection_number(
    connection_id: UUID,
    number_id: UUID,
    principal: Annotated[Principal, Depends(connections_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> None:
    """Remove a number from Hail. It stays with your phone system and your
    provider; calls stop reaching Hail agents."""
    lk = _livekit_or_503(lk)
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    number = await _number_or_404(db, row, number_id)
    number_id, e164 = number.id, number.e164
    was_released = number.provisioning_state == "released"
    try:
        await remove_connected_number(db, lk, number)
    except ConnectedNumberError as exc:
        raise _http_error(exc) from exc
    if not was_released:
        await _audit(
            principal, "number.release", "phone_number", number_id, {"e164": e164}
        )
