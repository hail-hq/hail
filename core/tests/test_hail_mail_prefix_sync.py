"""The old and new hail-mail prefix columns hold the same values.

``local_prefix_workspace`` is the new name of ``local_prefix_user`` and
``local_prefix_name`` the new name of ``local_prefix_org``. The website still
writes the old pair, so these tests write raw SQL the way it does.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from hailhq.core.models import (
    HAIL_MAIL_SYNC_FUNCTION,
    HAIL_MAIL_SYNC_TRIGGERS,
    RetiredHailMailAddress,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = (
    Path(__file__).parents[2] / "api/migrations/versions/0087_hail_mail_prefix_names.py"
)
spec = importlib.util.spec_from_file_location("hail_mail_prefix_names", MIGRATION)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

PAIRS = "local_prefix_user, local_prefix_org, local_prefix_workspace, local_prefix_name"


async def _insert(session, columns: dict, org=None) -> uuid.UUID:
    """Insert a hail-mail row the way a raw-SQL writer does."""
    row_id = uuid.uuid4()
    names = ", ".join(columns)
    values = ", ".join(f":{name}" for name in columns)
    await session.execute(
        text(
            "INSERT INTO email_domains (id, organization_id, kind, domain, "
            f"verification_status, {names}) VALUES (:id, :org, 'hail_mail', "
            f":domain, 'verified', {values})"
        ),
        {
            "id": row_id,
            "org": org or uuid.uuid4(),
            "domain": f"{uuid.uuid4().hex[:8]}@mail.hail.so",
            **columns,
        },
    )
    await session.commit()
    return row_id


async def _update(session, row_id, columns: dict) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in columns)
    await session.execute(
        text(f"UPDATE email_domains SET {sets} WHERE id = :id"),
        {"id": row_id, **columns},
    )
    await session.commit()


async def _pairs(session, row_id) -> tuple:
    row = await session.execute(
        text(f"SELECT {PAIRS} FROM email_domains WHERE id = :id"), {"id": row_id}
    )
    return tuple(row.one())


def _squash(sql: str) -> str:
    return " ".join(sql.split())


def test_trigger_text_matches_the_code() -> None:
    assert _squash(migration.SYNC_FUNCTION) == _squash(HAIL_MAIL_SYNC_FUNCTION)
    assert [_squash(t) for t in migration.SYNC_TRIGGERS] == [
        _squash(t) for t in HAIL_MAIL_SYNC_TRIGGERS
    ]


@pytest.mark.asyncio
async def test_sync_triggers_fire_first(async_session) -> None:
    """Postgres fires BEFORE row triggers in name order."""
    rows = await async_session.execute(text("""
            SELECT c.relname, t.tgname
              FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
             WHERE NOT t.tgisinternal
               AND c.relname IN ('email_domains', 'retired_hail_mail_addresses')
               AND t.tgtype & 2 = 2
             ORDER BY c.relname, t.tgname COLLATE "C"
            """))
    first: dict[str, str] = {}
    for table, name in rows:
        first.setdefault(table, name)
    assert first == {
        "email_domains": "email_domains_00_sync_prefix_columns",
        "retired_hail_mail_addresses": (
            "retired_hail_mail_addresses_00_sync_prefix_columns"
        ),
    }


@pytest.mark.asyncio
async def test_old_writer_insert_fills_the_new_columns(async_session) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    assert await _pairs(async_session, row_id) == ("acme", "amber", "acme", "amber")


@pytest.mark.asyncio
async def test_new_writer_insert_fills_the_old_columns(async_session) -> None:
    row_id = await _insert(
        async_session,
        {"local_prefix_workspace": "acme", "local_prefix_name": "amber"},
    )
    assert await _pairs(async_session, row_id) == ("acme", "amber", "acme", "amber")


@pytest.mark.asyncio
async def test_insert_with_both_pairs_keeps_the_new_pair(async_session) -> None:
    row_id = await _insert(
        async_session,
        {
            "local_prefix_user": "globex",
            "local_prefix_org": "heron",
            "local_prefix_workspace": "acme",
            "local_prefix_name": "amber",
        },
    )
    assert await _pairs(async_session, row_id) == ("acme", "amber", "acme", "amber")


@pytest.mark.asyncio
async def test_old_writer_update_moves_the_new_columns(async_session) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    await _update(async_session, row_id, {"local_prefix_user": "acme-labs"})
    await _update(async_session, row_id, {"local_prefix_org": "heron"})
    assert await _pairs(async_session, row_id) == (
        "acme-labs",
        "heron",
        "acme-labs",
        "heron",
    )


@pytest.mark.asyncio
async def test_new_writer_update_moves_the_old_columns(async_session) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    await _update(async_session, row_id, {"local_prefix_workspace": "acme-labs"})
    await _update(async_session, row_id, {"local_prefix_name": "heron"})
    assert await _pairs(async_session, row_id) == (
        "acme-labs",
        "heron",
        "acme-labs",
        "heron",
    )


@pytest.mark.asyncio
async def test_update_that_changes_both_pairs_keeps_the_new_pair(
    async_session,
) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    await _update(
        async_session,
        row_id,
        {
            "local_prefix_user": "globex",
            "local_prefix_workspace": "acme-labs",
            "local_prefix_org": "heron",
            "local_prefix_name": "amber-heron-42",
        },
    )
    assert await _pairs(async_session, row_id) == (
        "acme-labs",
        "amber-heron-42",
        "acme-labs",
        "amber-heron-42",
    )


@pytest.mark.asyncio
async def test_update_of_other_columns_keeps_the_prefixes(async_session) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    await _update(async_session, row_id, {"forward_rate_per_hour": 5})
    assert await _pairs(async_session, row_id) == ("acme", "amber", "acme", "amber")


@pytest.mark.asyncio
async def test_custom_rows_keep_every_prefix_null(async_session) -> None:
    row_id = uuid.uuid4()
    await async_session.execute(
        text(
            "INSERT INTO email_domains (id, organization_id, kind, domain) "
            "VALUES (:id, :org, 'custom', 'acme.example.com')"
        ),
        {"id": row_id, "org": uuid.uuid4()},
    )
    await async_session.commit()
    assert await _pairs(async_session, row_id) == (None, None, None, None)


@pytest.mark.asyncio
async def test_retired_rows_hold_both_pairs(async_session) -> None:
    # The 0070 retire trigger writes the old pair only.
    org = uuid.uuid4()
    row_id = await _insert(
        async_session,
        {"local_prefix_workspace": "acme", "local_prefix_name": "amber"},
        org=org,
    )
    await _update(async_session, row_id, {"local_prefix_workspace": "acme-labs"})
    # The ORM writes the new pair only.
    async_session.add(
        RetiredHailMailAddress(
            local_prefix_workspace="globex",
            local_prefix_name="heron",
            organization_id=org,
        )
    )
    await async_session.commit()

    rows = await async_session.execute(
        text(f"SELECT {PAIRS} FROM retired_hail_mail_addresses ORDER BY 1")
    )
    assert [tuple(r) for r in rows] == [
        ("acme", "amber", "acme", "amber"),
        ("globex", "heron", "globex", "heron"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("head_column", ["local_prefix_user", "local_prefix_workspace"])
async def test_guard_refuses_a_role_name_from_either_writer(
    async_session, head_column
) -> None:
    with pytest.raises(IntegrityError) as refused:
        await _insert(async_session, {head_column: "admin", "local_prefix_org": "x"})
    assert refused.value.orig.sqlstate == "23514"
    await async_session.rollback()

    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    with pytest.raises(IntegrityError) as refused:
        await _update(async_session, row_id, {head_column: "postmaster"})
    assert refused.value.orig.sqlstate == "23514"


@pytest.mark.asyncio
@pytest.mark.parametrize("head_column", ["local_prefix_user", "local_prefix_workspace"])
async def test_guard_refuses_a_retired_head_from_either_writer(
    async_session, head_column
) -> None:
    row_id = await _insert(
        async_session, {"local_prefix_user": "acme", "local_prefix_org": "amber"}
    )
    await _update(async_session, row_id, {head_column: "acme-labs"})

    with pytest.raises(IntegrityError) as refused:
        await _insert(
            async_session, {head_column: "acme", "local_prefix_name": "heron"}
        )
    assert refused.value.orig.sqlstate == "23505"
    await async_session.rollback()

    other = await _insert(
        async_session, {head_column: "globex", "local_prefix_name": "heron"}
    )
    with pytest.raises(IntegrityError) as refused:
        await _update(async_session, other, {head_column: "acme"})
    assert refused.value.orig.sqlstate == "23505"


@pytest.mark.asyncio
@pytest.mark.parametrize("head_column", ["local_prefix_user", "local_prefix_workspace"])
async def test_head_is_unique_for_either_writer(async_session, head_column) -> None:
    await _insert(async_session, {head_column: "acme", "local_prefix_org": "amber"})
    with pytest.raises(IntegrityError) as refused:
        await _insert(
            async_session, {head_column: "acme", "local_prefix_name": "heron"}
        )
    assert refused.value.orig.sqlstate == "23505"
