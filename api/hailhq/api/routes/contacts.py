"""Org contacts: computed member∪manual list, manual CRUD, member phones.

GET    /contacts        - cursor-paginated union list (members live from
                           users/members; manual rows)
POST   /contacts        - create manual contact (phone and/or email)
PATCH  /contacts/{id}   - manual only; member:* ids are managed via membership
DELETE /contacts/{id}   - manual only
PUT    /members/{user_id|me}/phone - self or org owner/admin
DELETE /members/{user_id|me}/phone
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.contacts import MEMBER_ID_PREFIX, contact_to_entry, contacts_union_stmt
from hailhq.core.db import get_session
from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import (
    ContactCreate,
    ContactEntry,
    ContactListResponse,
    ContactPatch,
    MemberPhonePut,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["contacts"])

_MEMBER_ID_DETAIL = "member contacts are managed via membership"
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 500


def _manual_uuid_or_422(contact_id: str) -> UUID:
    if contact_id.startswith(MEMBER_ID_PREFIX):
        raise unprocessable(_MEMBER_ID_DETAIL, loc=["path", "contact_id"])
    try:
        return UUID(contact_id)
    except ValueError as exc:
        raise unprocessable(
            f"invalid contact id: {contact_id}", loc=["path", "contact_id"]
        ) from exc


async def _get_manual_or_404(
    db: AsyncSession, org_id: UUID, contact_id: str
) -> Contact:
    cid = _manual_uuid_or_422(contact_id)
    row = (
        await db.execute(
            select(Contact).where(Contact.id == cid, Contact.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="contact not found"
        )
    return row


async def _member_role(db: AsyncSession, org_id: UUID, user_id: UUID) -> str | None:
    """Org role for ``user_id``, or ``None`` if they aren't a member of
    ``org_id``. Shared by the phone-target self-or-admin check below —
    both the target's and the caller's role are the same lookup."""
    return (
        await db.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()


@router.get(
    "/contacts",
    response_model=ContactListResponse,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def list_contacts(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> ContactListResponse:
    """List contacts for the caller's organization, newest first.

    Merges two sources into one list: org members (kind="member", ids
    prefixed "member:", managed via membership — not editable here) and
    manually-created contacts (kind="manual", editable via PATCH/DELETE
    /contacts/{contact_id}). Cursor-paginated; q does a substring search
    over name/phone/email.
    """
    u = contacts_union_stmt(principal.organization_id, q).subquery()
    rows, next_cursor = await fetch_cursor_page(
        db,
        select(u),
        u.c.created_at,
        u.c.id_uuid,
        cursor=cursor,
        limit=limit,
        scalars=False,
    )
    items = [
        ContactEntry(
            id=row.id_text,
            kind=row.kind,
            name=row.name,
            phone_e164=row.phone_e164,
            email=row.email,
            role=row.role,
        )
        for row in rows
    ]
    return ContactListResponse(items=items, next_cursor=next_cursor)


@router.post(
    "/contacts",
    response_model=ContactEntry,
    status_code=http_status.HTTP_201_CREATED,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def create_contact(
    body: ContactCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
    """Create a manual contact with a phone and/or an email.

    Requires at least one of phone_e164 or email. Fails with 409 if a
    contact with the same phone or email already exists in this
    organization.
    """
    row = Contact(
        organization_id=principal.organization_id,
        name=body.name,
        phone_e164=body.phone_e164,
        email=body.email,
        created_by=principal.user_id,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="a contact with that phone or email already exists",
        ) from exc
    await db.refresh(row)
    return contact_to_entry(row)


@router.patch(
    "/contacts/{contact_id}",
    response_model=ContactEntry,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def patch_contact(
    contact_id: str,
    body: ContactPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
    """Update a manual contact's fields. Only fields present in the body change.

    Manual contacts only — a member: id (org members synced from
    membership) returns 422; edit those via the membership APIs instead.
    The contact must still have at least one of phone_e164 or email after
    the update. Fails with 409 on a duplicate phone/email.
    """
    row = await _get_manual_or_404(db, principal.organization_id, contact_id)
    data = body.model_dump(exclude_unset=True)
    next_phone = data.get("phone_e164", row.phone_e164)
    next_email = data.get("email", row.email)
    if next_phone is None and next_email is None:
        raise unprocessable("a contact needs at least one of phone_e164 or email")
    for field, value in data.items():
        setattr(row, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="a contact with that phone or email already exists",
        ) from exc
    await db.refresh(row)
    return contact_to_entry(row)


@router.delete(
    "/contacts/{contact_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def delete_contact(
    contact_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Permanently remove a manual contact.

    Manual contacts only — a member: id returns 422; org members are
    removed via the membership APIs, not this route. Irreversible.
    """
    row = await _get_manual_or_404(db, principal.organization_id, contact_id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


async def _resolve_phone_target(
    db: AsyncSession, principal: Principal, user_id: str
) -> UUID:
    """Resolve `me`/UUID, enforce self-or-admin, and same-org membership."""
    if user_id == "me":
        if principal.user_id is None:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="this credential has no user identity; pass an explicit user id",
            )
        target = principal.user_id
    else:
        try:
            target = UUID(user_id)
        except ValueError as exc:
            raise unprocessable(
                f"invalid user id: {user_id}", loc=["path", "user_id"]
            ) from exc

    target_role = await _member_role(db, principal.organization_id, target)
    if target_role is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="member not found"
        )

    if target != principal.user_id:
        caller_role = await _member_role(
            db, principal.organization_id, principal.user_id
        )
        if caller_role not in ("owner", "admin"):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="only the member themselves or an org owner/admin can set a member phone",
            )
    return target


@router.put(
    "/members/{user_id}/phone",
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def put_member_phone(
    user_id: str,
    body: MemberPhonePut,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    """Set an org member's phone number.

    Pass user_id="me" to set your own, or a member's user id — setting
    another member's phone requires the caller to be an org owner or
    admin. Returns 404 if the target is not a member of this organization.
    """
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(
        update(User).where(User.id == target).values(phone_number=body.phone_e164)
    )
    await db.commit()
    return {"user_id": str(target), "phone_e164": body.phone_e164}


@router.delete(
    "/members/{user_id}/phone",
    status_code=http_status.HTTP_204_NO_CONTENT,
    responses=GENERAL_RATE_LIMITED_RESPONSES,
)
async def delete_member_phone(
    user_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Clear an org member's phone number.

    Pass user_id="me" to clear your own, or a member's user id — clearing
    another member's phone requires the caller to be an org owner or
    admin. Returns 404 if the target is not a member of this organization.
    """
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(update(User).where(User.id == target).values(phone_number=None))
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
