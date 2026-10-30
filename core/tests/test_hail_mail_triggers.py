"""Rules the database holds for every hail-mail writer."""

import uuid

import pytest
from hailhq.core.hail_mail import ROLE_MAILBOXES
from hailhq.core.models import EmailDomain, RetiredHailMailAddress
from hailhq.core.schemas import RESERVED_USER_PREFIXES
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError


def _row(org_id, head="alice", tail="acme") -> EmailDomain:
    return EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=f"{head}+{tail}@mail.hail.so",
        local_prefix_user=head,
        local_prefix_org=tail,
        verification_status="verified",
        provider="ses",
    )


async def _add(session, row) -> EmailDomain:
    session.add(row)
    await session.commit()
    return row


async def _set_head(session, row_id, head) -> None:
    await session.execute(
        update(EmailDomain)
        .where(EmailDomain.id == row_id)
        .values(local_prefix_user=head, domain=f"{head}+acme@mail.hail.so")
    )
    await session.commit()


async def _retired(session) -> list[tuple]:
    rows = await session.execute(
        select(
            RetiredHailMailAddress.local_prefix_user,
            RetiredHailMailAddress.local_prefix_org,
            RetiredHailMailAddress.organization_id,
        )
    )
    return [tuple(r) for r in rows]


def _sqlstate(exc: pytest.ExceptionInfo) -> str:
    return exc.value.orig.sqlstate


def test_role_names_are_reserved() -> None:
    assert ROLE_MAILBOXES <= RESERVED_USER_PREFIXES
    assert {"noreply", "forwarder"} <= RESERVED_USER_PREFIXES


@pytest.mark.asyncio
async def test_head_change_records_the_old_address(async_session) -> None:
    org = uuid.uuid4()
    row = await _add(async_session, _row(org))

    await _set_head(async_session, row.id, "bob")

    assert await _retired(async_session) == [("alice", "acme", org)]


@pytest.mark.asyncio
async def test_delete_records_the_address(async_session) -> None:
    org = uuid.uuid4()
    row = await _add(async_session, _row(org))

    await async_session.execute(delete(EmailDomain).where(EmailDomain.id == row.id))
    await async_session.commit()

    assert await _retired(async_session) == [("alice", "acme", org)]


@pytest.mark.asyncio
async def test_tail_change_and_custom_rows_record_nothing(async_session) -> None:
    row = await _add(async_session, _row(uuid.uuid4()))
    custom = await _add(
        async_session,
        EmailDomain(
            organization_id=uuid.uuid4(),
            kind="custom",
            domain="acme.example.com",
            verification_status="verified",
            provider="ses",
        ),
    )

    await async_session.execute(
        update(EmailDomain)
        .where(EmailDomain.id == row.id)
        .values(local_prefix_org="sales", domain="alice+sales@mail.hail.so")
    )
    await async_session.execute(delete(EmailDomain).where(EmailDomain.id == custom.id))
    await async_session.commit()

    assert await _retired(async_session) == []


@pytest.mark.asyncio
async def test_other_org_cannot_take_a_retired_head(async_session) -> None:
    row = await _add(async_session, _row(uuid.uuid4()))
    await _set_head(async_session, row.id, "bob")

    with pytest.raises(IntegrityError) as insert_error:
        await _add(async_session, _row(uuid.uuid4()))
    assert _sqlstate(insert_error) == "23505"
    await async_session.rollback()

    other = await _add(async_session, _row(uuid.uuid4(), head="carol"))
    with pytest.raises(IntegrityError) as update_error:
        await _set_head(async_session, other.id, "alice")
    assert _sqlstate(update_error) == "23505"


@pytest.mark.asyncio
async def test_same_org_may_reuse_its_own_head(async_session) -> None:
    row = await _add(async_session, _row(uuid.uuid4()))
    row_id = row.id
    await _set_head(async_session, row_id, "bob")

    await _set_head(async_session, row_id, "alice")

    head = await async_session.execute(
        select(EmailDomain.local_prefix_user).where(EmailDomain.id == row_id)
    )
    assert head.scalar_one() == "alice"


@pytest.mark.asyncio
async def test_org_that_held_a_head_may_return_to_it(async_session) -> None:
    # A migration rename leaves the renamed org's record on the keeper's head.
    keeper = await _add(async_session, _row(uuid.uuid4()))
    keeper_id = keeper.id
    async_session.add(
        RetiredHailMailAddress(
            local_prefix_user="alice",
            local_prefix_org="globex",
            organization_id=uuid.uuid4(),
        )
    )
    await async_session.commit()

    await _set_head(async_session, keeper_id, "bob")
    await _set_head(async_session, keeper_id, "alice")


@pytest.mark.asyncio
async def test_role_name_is_refused_as_a_new_head(async_session) -> None:
    with pytest.raises(IntegrityError) as insert_error:
        await _add(async_session, _row(uuid.uuid4(), head="admin"))
    assert _sqlstate(insert_error) == "23514"
    await async_session.rollback()

    row = await _add(async_session, _row(uuid.uuid4()))
    with pytest.raises(IntegrityError) as update_error:
        await _set_head(async_session, row.id, "postmaster")
    assert _sqlstate(update_error) == "23514"


@pytest.mark.asyncio
async def test_an_org_holds_one_address(async_session) -> None:
    org = uuid.uuid4()
    await _add(async_session, _row(org))

    with pytest.raises(IntegrityError):
        await _add(async_session, _row(org, head="bob"))


@pytest.mark.asyncio
async def test_a_head_belongs_to_one_org(async_session) -> None:
    await _add(async_session, _row(uuid.uuid4()))

    with pytest.raises(IntegrityError):
        await _add(async_session, _row(uuid.uuid4(), tail="globex"))
