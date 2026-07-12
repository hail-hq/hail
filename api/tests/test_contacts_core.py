"""Contacts core: models (Task 1) and search_contacts union (Task 2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from hailhq.core.contacts import search_contacts
from hailhq.core.models import Contact, OrganizationMember, User


async def test_contact_model_round_trip(async_session, org_and_key):
    org_id, _api_key, _plaintext = org_and_key
    row = Contact(organization_id=org_id, name="Maya", phone_e164="+14155550100")
    async_session.add(row)
    await async_session.commit()

    got = (await async_session.execute(select(Contact))).scalar_one()
    assert got.name == "Maya"
    assert got.email is None
    assert got.id is not None


async def test_user_model_maps_users_table(async_session):
    uid = uuid.uuid4()
    async_session.add(User(id=uid, name="Ada", email=f"{uid}@example.com"))
    await async_session.commit()
    got = (await async_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert got.phone_number is None


async def _seed_member(session, org_id, *, name, email, phone=None, role="member"):
    uid = uuid.uuid4()
    session.add(User(id=uid, name=name, email=email, phone_number=phone))
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            organization_id=org_id,
            user_id=uid,
            role=role,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return uid


async def test_union_members_first_then_manual(async_session, org_and_key):
    org_id, _api_key, _plaintext = org_and_key
    uid = await _seed_member(
        async_session, org_id, name="Ada", email="ada@acme.com", phone="+15550001"
    )
    async_session.add(Contact(organization_id=org_id, name="Maya", email="maya@x.com"))
    await async_session.commit()

    entries = await search_contacts(async_session, org_id)
    kinds = [e.kind for e in entries]
    assert kinds == ["member", "manual"]
    member = entries[0]
    assert member.id == f"member:{uid}"
    assert member.phone_e164 == "+15550001"
    assert member.role == "member"
    assert entries[1].phone_e164 is None and entries[1].email == "maya@x.com"


async def test_q_filters_both_branches_case_insensitive(async_session, org_and_key):
    org_id, _api_key, _plaintext = org_and_key
    await _seed_member(async_session, org_id, name="Ada Lovelace", email="ada@acme.com")
    async_session.add(
        Contact(organization_id=org_id, name="Maya", phone_e164="+14155550100")
    )
    async_session.add(Contact(organization_id=org_id, name="Bob", email="bob@x.com"))
    await async_session.commit()

    assert [e.name for e in await search_contacts(async_session, org_id, q="ada")] == [
        "Ada Lovelace"
    ]
    assert [e.name for e in await search_contacts(async_session, org_id, q="MAYA")] == [
        "Maya"
    ]
    assert [
        e.name for e in await search_contacts(async_session, org_id, q="4155550100")
    ] == ["Maya"]


async def test_org_isolation(async_session, org_and_key):
    org_id, _api_key, _plaintext = org_and_key
    other_org = uuid.uuid4()
    async_session.add(Contact(organization_id=other_org, name="Other", email="o@x.com"))
    await async_session.commit()
    assert await search_contacts(async_session, org_id) == []
