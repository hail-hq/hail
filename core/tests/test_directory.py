"""Recipient-directory lookups: org scoping is the load-bearing property."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from hailhq.core.directory import (
    find_by_phone,
    list_directory,
    resolve_member_emails,
)
from hailhq.core.models import Contact, OrganizationMember, User


async def _add_member(session, org_id, name, email, phone_number=None):
    user = User(
        id=uuid.uuid4(),
        name=name,
        email=email,
        phone_number=phone_number,
        created_at=datetime.now(timezone.utc),
    )
    session.add(user)
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org_id,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return user


async def _add_contact(session, org_id, name, *, email=None, phone_e164=None):
    contact = Contact(
        id=uuid.uuid4(),
        organization_id=org_id,
        name=name,
        email=email,
        phone_e164=phone_e164,
    )
    session.add(contact)
    await session.commit()
    return contact


async def test_list_directory_scoped_to_org(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    await _add_member(async_session, org_b, "Bob", "bob@b.test")

    entries = await list_directory(async_session, org_a)
    assert [e.name for e in entries] == ["Alice"]
    assert entries[0].has_email is True
    assert entries[0].has_phone is False  # no phone_number set on this member
    assert entries[0].source == "member"


async def test_list_directory_member_with_phone_number(async_session):
    org = uuid.uuid4()
    await _add_member(
        async_session, org, "Priya", "priya@a.test", phone_number="+15551234567"
    )

    entries = await list_directory(async_session, org)
    assert [e.name for e in entries] == ["Priya"]
    assert entries[0].has_email is True
    assert entries[0].has_phone is True
    assert entries[0].source == "member"


async def test_list_directory_lists_members_only(async_session):
    """Agent sends reach members only, so contacts are not listed."""
    org = uuid.uuid4()
    await _add_member(async_session, org, "Alice", "alice@a.test")
    await _add_contact(async_session, org, "Vendor Co", email="billing@vendor.test")

    entries = await list_directory(async_session, org)
    assert [e.name for e in entries] == ["Alice"]


async def test_list_directory_empty_org(async_session):
    assert await list_directory(async_session, uuid.uuid4()) == []


async def test_resolve_member_emails_case_insensitive(async_session):
    org = uuid.uuid4()
    await _add_member(async_session, org, "Sarah Chen", "sarah@x.test")
    assert await resolve_member_emails(async_session, org, "  sarah chen ") == [
        "sarah@x.test"
    ]


async def test_resolve_member_emails_never_crosses_orgs(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    assert await resolve_member_emails(async_session, org_b, "Alice") == []


async def test_resolve_member_emails_returns_all_matches(async_session):
    org = uuid.uuid4()
    await _add_member(async_session, org, "Sam", "sam1@x.test")
    await _add_member(async_session, org, "sam", "sam2@x.test")
    assert sorted(await resolve_member_emails(async_session, org, "Sam")) == [
        "sam1@x.test",
        "sam2@x.test",
    ]


async def test_find_by_phone_prefers_members_and_stays_in_org(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(
        async_session, org_a, "Priya", "p@a.test", phone_number="+15551234567"
    )
    await _add_contact(async_session, org_a, "Priya Mobile", phone_e164="+15551234567")
    await _add_contact(
        async_session, org_a, "Maya", email="m@a.test", phone_e164="+15559876543"
    )

    member = await find_by_phone(async_session, org_a, "+15551234567")
    assert (member.name, member.source) == ("Priya", "member")
    contact = await find_by_phone(async_session, org_a, "+15559876543")
    assert (contact.name, contact.source, contact.has_email) == (
        "Maya",
        "contact",
        True,
    )
    assert await find_by_phone(async_session, org_b, "+15551234567") is None
    assert await find_by_phone(async_session, org_a, "+15550000000") is None


async def test_find_by_phone_names_nobody_for_a_shared_number(async_session):
    org = uuid.uuid4()
    await _add_member(
        async_session, org, "Ana", "ana@a.test", phone_number="+15550001111"
    )
    await _add_member(
        async_session, org, "Ben", "ben@a.test", phone_number="+15550001111"
    )

    assert await find_by_phone(async_session, org, "+15550001111") is None
