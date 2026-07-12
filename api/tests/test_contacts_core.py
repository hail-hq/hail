"""Contacts core: models (Task 1) and search_contacts union (Task 2)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from hailhq.core.models import Contact, User


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
