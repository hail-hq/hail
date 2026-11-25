"""Connected phone systems: a customer's PBX sends calls to Hail agents.

POST   /sip-connections                                   create (password shown once)
GET    /sip-connections                                   list, newest first
GET    /sip-connections/{id}                              one connection
PATCH  /sip-connections/{id}                              label, PBX address, credentials
POST   /sip-connections/{id}/rotate-password              new password (shown once)
DELETE /sip-connections/{id}                              delete (no numbers left)
POST   /sip-connections/{id}/numbers                      add a number (unverified)
POST   /sip-connections/{id}/numbers/{number_id}/verify   ownership check call
DELETE /sip-connections/{id}/numbers/{number_id}          remove a number from Hail

Thin routes over ``hailhq.api.connected_numbers``. Passwords appear only in
the create and rotate responses: no idempotency store, no audit payload.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
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
from hailhq.core.db import get_session
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import PhoneNumber, SipConnection
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


def _response(row: SipConnection) -> SipConnectionResponse:
    try:
        last4 = decrypt_secret(row.encrypted_inbound_password)[-4:]
    except SecretKeyMissing as exc:
        raise _not_configured() from exc
    return SipConnectionResponse(
        id=row.id,
        label=row.label,
        default_region=row.default_region,
        sip_host=settings.livekit_sip_host,
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


@router.post(
    "",
    response_model=SipConnectionCreated,
    status_code=http_status.HTTP_201_CREATED,
    responses=_ERROR_RESPONSES,
)
async def create_sip_connection(
    body: SipConnectionCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> SipConnectionCreated:
    """Connect your phone system (PBX). Add the returned SIP server, username
    and password to it as a generic SIP trunk. The password is shown only in
    this response; rotate it to get a new one. With ``pbx_host``, agents can
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
    return SipConnectionCreated(**_response(row).model_dump(), password=password)


@router.get("", response_model=SipConnectionListResponse)
async def list_sip_connections(
    principal: Annotated[Principal, Depends(get_current_principal)],
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
        items=[_response(r) for r in rows], next_cursor=next_cursor
    )


@router.get(
    "/{connection_id}", response_model=SipConnectionResponse, responses=_ERROR_RESPONSES
)
async def get_sip_connection(
    connection_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> SipConnectionResponse:
    """Read one connected phone system."""
    row = await _connection_or_404(db, principal.organization_id, connection_id)
    return _response(row)


@router.patch(
    "/{connection_id}", response_model=SipConnectionResponse, responses=_ERROR_RESPONSES
)
async def update_sip_connection(
    connection_id: UUID,
    body: SipConnectionPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
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
    return _response(row)


@router.post(
    "/{connection_id}/rotate-password",
    response_model=SipConnectionPassword,
    responses=_ERROR_RESPONSES,
)
async def rotate_sip_connection_password(
    connection_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
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
    return SipConnectionPassword(password=password)


@router.delete(
    "/{connection_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=_ERROR_RESPONSES,
)
async def delete_sip_connection(
    connection_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
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
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    lk: Annotated[LiveKitClient | None, Depends(get_livekit_optional)],
) -> PhoneNumberResponse:
    """Add a number your phone system holds. It starts ``unverified``: send
    its incoming calls to Hail, then run the ownership check (verify). An
    unchecked number is removed after 7 days, or when someone else buys or
    connects it."""
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
    principal: Annotated[Principal, Depends(get_current_principal)],
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
    principal: Annotated[Principal, Depends(get_current_principal)],
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
