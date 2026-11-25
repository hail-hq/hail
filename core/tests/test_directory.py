"""Recipient-directory lookups: org scoping is the load-bearing property."""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import datetime, timezone

import pytest
from hailhq.core import directory
from hailhq.core.directory import (
    entry_email,
    find_by_phone,
    has_member_phone,
    list_directory,
    resolve_contact_emails,
    resolve_member_emails,
)
from hailhq.core.models import Contact, OrganizationMember, User
from sqlalchemy.exc import ProgrammingError


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


async def test_has_member_phone_ignores_name_and_sharing(async_session):
    """Setup help goes to any member with the number: unnamed, or sharing
    it with another member. A contact, or another org's member, is not one."""
    org, other = uuid.uuid4(), uuid.uuid4()
    await _add_member(async_session, org, "", "a@a.test", phone_number="+15550002222")
    await _add_member(
        async_session, org, "Ana", "b@a.test", phone_number="+15550003333"
    )
    await _add_member(
        async_session, org, "Ben", "c@a.test", phone_number="+15550003333"
    )
    await _add_member(
        async_session, other, "Cy", "d@a.test", phone_number="+15550004444"
    )
    await _add_contact(async_session, org, "Dee", phone_e164="+15550005555")

    assert await has_member_phone(async_session, org, "+15550002222")
    assert await has_member_phone(async_session, org, "+15550003333")
    assert not await has_member_phone(async_session, org, "+15550004444")
    assert not await has_member_phone(async_session, org, "+15550005555")


async def test_resolve_contact_emails_by_name_in_org_only(async_session):
    org, other = uuid.uuid4(), uuid.uuid4()
    await _add_contact(async_session, org, "Sam Lee", email="sam@example.com")
    await _add_contact(async_session, org, "Sam Lee", phone_e164="+14155550142")
    await _add_contact(async_session, other, "Sam Lee", email="sam@other.example")
    # The contact with no email is left out; the other org's never shows.
    assert await resolve_contact_emails(async_session, org, "  sam lee ") == [
        "sam@example.com"
    ]
    assert await resolve_contact_emails(async_session, org, "Nobody") == []


class _SqlError(Exception):
    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _FailingSession:
    """Every query fails with ``sqlstate``; 42P01 is a self-host with no
    website-owned member tables."""

    def __init__(self, sqlstate):
        self.sqlstate = sqlstate

    def begin_nested(self):
        return contextlib.nullcontext()

    async def execute(self, _stmt):
        raise ProgrammingError("SELECT", {}, _SqlError(self.sqlstate))


async def test_member_lookup_is_empty_without_member_tables():
    session = _FailingSession("42P01")
    assert await resolve_member_emails(session, uuid.uuid4(), "Sam Lee") == []


async def test_member_lookup_raises_other_sql_errors():
    with pytest.raises(ProgrammingError):
        await resolve_member_emails(_FailingSession("42703"), uuid.uuid4(), "Sam")


async def _email_for_number(session, org, phone):
    return await entry_email(
        session, org, phone, await find_by_phone(session, org, phone)
    )


async def test_entry_email_goes_by_number_members_first(async_session):
    org = uuid.uuid4()
    await _add_member(async_session, org, "Sam Lee", "sam@team.test", "+15550001111")
    await _add_contact(
        async_session,
        org,
        "Sam Lee",
        email="sam@northgate.test",
        phone_e164="+15550002222",
    )
    await _add_contact(async_session, org, "Alex Kim", phone_e164="+15550003333")

    assert (
        await _email_for_number(async_session, org, "+15550001111") == "sam@team.test"
    )
    assert (
        await _email_for_number(async_session, org, "+15550002222")
        == "sam@northgate.test"
    )
    # A saved number with no email, or no saved number, gives nothing.
    assert await _email_for_number(async_session, org, "+15550003333") is None
    assert await _email_for_number(async_session, org, "+15550009999") is None
    assert await _email_for_number(async_session, uuid.uuid4(), "+15550002222") is None


async def test_missing_member_tables_warn_once_per_process(caplog, monkeypatch):
    monkeypatch.setattr(directory, "_warned_no_member_tables", False)
    with caplog.at_level(logging.DEBUG, logger="hailhq.core.directory"):
        for _ in range(3):
            await resolve_member_emails(_FailingSession("42P01"), uuid.uuid4(), "Sam")
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]
