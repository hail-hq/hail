"""Schema-level tests for the email_accounts table and emails columns."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email, EmailAccount


async def _mk_account(session: AsyncSession, org_id, address="alice@gmail.com"):
    acct = EmailAccount(
        organization_id=org_id,
        provider="gmail",
        email_address=address,
        display_name="Alice",
        provider_user_id="google-sub-123",
        scopes=[
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
        ],
        encrypted_refresh_token="gAAAAAB-ciphertext",
    )
    session.add(acct)
    await session.commit()
    await session.refresh(acct)
    return acct


async def test_email_account_defaults(async_session: AsyncSession) -> None:
    acct = await _mk_account(async_session, uuid.uuid4())
    assert acct.status == "active"
    assert acct.provider == "gmail"
    assert acct.created_at is not None


async def test_email_address_globally_unique(async_session: AsyncSession) -> None:
    await _mk_account(async_session, uuid.uuid4())
    with pytest.raises(IntegrityError):
        await _mk_account(async_session, uuid.uuid4())  # same address, other org
    await async_session.rollback()


async def test_email_row_via_account(async_session: AsyncSession) -> None:
    org = uuid.uuid4()
    acct = await _mk_account(async_session, org)
    email = Email(
        organization_id=org,
        email_account_id=acct.id,
        from_address="alice@gmail.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        provider="gmail",
        provider_thread_id="thread-abc",
    )
    async_session.add(email)
    await async_session.commit()
    await async_session.refresh(email)
    assert email.email_domain_id is None
    assert email.provider_thread_id == "thread-abc"


async def test_outbound_requires_some_sender(async_session: AsyncSession) -> None:
    email = Email(
        organization_id=uuid.uuid4(),
        from_address="x@y.com",
        to_addresses=["b@c.com"],
        subject="hi",
        body_text="hello",
    )
    async_session.add(email)
    with pytest.raises(IntegrityError):
        await async_session.commit()
    await async_session.rollback()
