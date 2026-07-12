"""Org contacts: computed member∪manual list, manual CRUD, member phones.

GET    /contacts        - union list (members live from users/members; manual rows)
POST   /contacts        - create manual contact (phone and/or email)
PATCH  /contacts/{id}   - manual only; member:* ids are managed via membership
DELETE /contacts/{id}   - manual only
PUT    /members/{user_id|me}/phone - self or org owner/admin
DELETE /members/{user_id|me}/phone
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.deps import Principal, get_current_principal
from hailhq.api.errors import unprocessable
from hailhq.core.contacts import search_contacts
from hailhq.core.db import get_session
from hailhq.core.models import Contact, OrganizationMember, User
from hailhq.core.schemas import (
    ContactCreate,
    ContactEntry,
    ContactListResponse,
    ContactPatch,
    MemberPhonePut,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contacts"])

_MEMBER_ID_DETAIL = "member contacts are managed via membership"


def _manual_uuid_or_422(contact_id: str) -> UUID:
    if contact_id.startswith("member:"):
        raise unprocessable(_MEMBER_ID_DETAIL, loc=["path", "contact_id"])
    try:
        return UUID(contact_id)
    except ValueError as exc:
        raise unprocessable(f"invalid contact id: {contact_id}") from exc


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
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="contact not found")
    return row


def _entry(row: Contact) -> ContactEntry:
    return ContactEntry(
        id=str(row.id), kind="manual", name=row.name,
        phone_e164=row.phone_e164, email=row.email, role=None,
    )


@router.get("/contacts", response_model=ContactListResponse)
async def list_contacts(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> ContactListResponse:
    items = await search_contacts(db, principal.organization_id, q=q, limit=limit)
    return ContactListResponse(items=items)


@router.post("/contacts", response_model=ContactEntry, status_code=http_status.HTTP_201_CREATED)
async def create_contact(
    body: ContactCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
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
    return _entry(row)


@router.patch("/contacts/{contact_id}", response_model=ContactEntry)
async def patch_contact(
    contact_id: str,
    body: ContactPatch,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> ContactEntry:
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
    return _entry(row)


@router.delete("/contacts/{contact_id}", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
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
            raise unprocessable(f"invalid user id: {user_id}") from exc

    target_role = (
        await db.execute(
            select(OrganizationMember.role).where(
                OrganizationMember.organization_id == principal.organization_id,
                OrganizationMember.user_id == target,
            )
        )
    ).scalar_one_or_none()
    if target_role is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="member not found")

    if target != principal.user_id:
        caller_role = (
            await db.execute(
                select(OrganizationMember.role).where(
                    OrganizationMember.organization_id == principal.organization_id,
                    OrganizationMember.user_id == principal.user_id,
                )
            )
        ).scalar_one_or_none()
        if caller_role not in ("owner", "admin"):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="only the member themselves or an org owner/admin can set a member phone",
            )
    return target


@router.put("/members/{user_id}/phone")
async def put_member_phone(
    user_id: str,
    body: MemberPhonePut,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(update(User).where(User.id == target).values(phone_number=body.phone_e164))
    await db.commit()
    return {"user_id": str(target), "phone_e164": body.phone_e164}


@router.delete("/members/{user_id}/phone", status_code=http_status.HTTP_204_NO_CONTENT)
async def delete_member_phone(
    user_id: str,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    target = await _resolve_phone_target(db, principal, user_id)
    await db.execute(update(User).where(User.id == target).values(phone_number=None))
    await db.commit()
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)
