"""Recipient-directory lookups: org scoping is the load-bearing property."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hailhq.core.directory import list_directory, resolve_member_emails
from hailhq.core.models import Contact, OrganizationMember, User
from sqlalchemy import text


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


async def test_list_directory_includes_manual_contacts(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    await _add_contact(async_session, org_a, "Vendor Co", email="billing@vendor.test")
    await _add_contact(async_session, org_a, "Maya", phone_e164="+15559876543")
    await _add_contact(async_session, org_b, "Other Org Contact", email="x@b.test")

    entries = await list_directory(async_session, org_a)
    assert [e.name for e in entries] == ["Alice", "Maya", "Vendor Co"]

    maya = next(e for e in entries if e.name == "Maya")
    assert maya.source == "contact"
    assert maya.has_email is False
    assert maya.has_phone is True

    vendor = next(e for e in entries if e.name == "Vendor Co")
    assert vendor.source == "contact"
    assert vendor.has_email is True
    assert vendor.has_phone is False


async def test_list_directory_tolerates_missing_contacts_table(async_session):
    """Self-host posture: no `contacts` table -> members-only, not a 500.

    Drops the real table (rather than mocking) so this exercises the
    actual asyncpg ``UndefinedTable`` -> ``ProgrammingError`` path.
    """
    org = uuid.uuid4()
    await _add_member(async_session, org, "Alice", "alice@a.test")
    await async_session.execute(text("DROP TABLE contacts"))
    await async_session.commit()

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
