"""Contacts core: models (Task 1) and search_contacts union (Task 2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from hailhq.core.contacts import search_contacts
from hailhq.core.models import Contact, User
from hailhq.core.testing.fixtures import seed_member as _seed_member


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
    async_session.add(
        User(
            id=uid,
            name="Ada",
            email=f"{uid}@example.com",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()
    got = (await async_session.execute(select(User).where(User.id == uid))).scalar_one()
    assert got.phone_number is None


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


async def test_q_search_manual_match_survives_member_starvation(
    async_session, org_and_key
):
    """Regression for the union-search starvation bug: with a members-first,
    per-branch-limited query, 12 name-matching members plus 1 name-matching
    manual contact at limit=10 dropped the manual contact entirely (the
    member branch alone filled the whole limit). The single ordered union
    (name asc) must let the alphabetically-earlier manual row compete for
    the limit on equal footing."""
    org_id, _api_key, _plaintext = org_and_key
    for i in range(12):
        await _seed_member(
            async_session,
            org_id,
            name=f"Zebra Corp {i:02d}",
            email=f"zebra{i}@acme.com",
        )
    async_session.add(
        Contact(organization_id=org_id, name="Alpha Corp", email="alpha@corp.com")
    )
    await async_session.commit()

    entries = await search_contacts(async_session, org_id, q="corp", limit=10)
    assert len(entries) == 10
    assert any(e.kind == "manual" and e.name == "Alpha Corp" for e in entries)


async def test_percent_and_underscore_match_literally(async_session, org_and_key):
    org_id, _api_key, _plaintext = org_and_key
    async_session.add(
        Contact(organization_id=org_id, name="100% Contact", phone_e164="+14155550001")
    )
    async_session.add(
        Contact(organization_id=org_id, name="Normal Contact", email="normal@x.com")
    )
    async_session.add(
        Contact(organization_id=org_id, name="j_hn Contact", email="jhn@x.com")
    )
    async_session.add(
        Contact(organization_id=org_id, name="john contact", email="john@x.com")
    )
    await async_session.commit()

    pct_results = await search_contacts(async_session, org_id, q="%")
    assert [e.name for e in pct_results] == ["100% Contact"]

    underscore_results = await search_contacts(async_session, org_id, q="j_hn")
    assert [e.name for e in underscore_results] == ["j_hn Contact"]
