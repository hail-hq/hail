"""Recipient-directory lookups: org scoping is the load-bearing property."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hailhq.core.directory import list_directory, resolve_member_emails
from hailhq.core.models import OrganizationMember, User


async def _add_member(session, org_id, name, email):
    user = User(
        id=uuid.uuid4(),
        name=name,
        email=email,
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


async def test_list_directory_scoped_to_org(async_session):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org_a, "Alice", "alice@a.test")
    await _add_member(async_session, org_b, "Bob", "bob@b.test")

    entries = await list_directory(async_session, org_a)
    assert [e.name for e in entries] == ["Alice"]
    assert entries[0].has_email is True
    assert entries[0].has_phone is False  # users table has no phone column
    assert entries[0].source == "member"


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
