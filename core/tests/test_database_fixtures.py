"""Exercise consecutive fixture resets, including committed schema changes."""

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module")
def table_oids():
    return []


@pytest.mark.parametrize("iteration", range(2))
async def test_committed_rows_are_cleared_without_recreating_tables(
    db, table_oids, iteration
):
    async with db.begin() as conn:
        oid = (
            await conn.execute(text("SELECT 'organizations'::regclass::oid"))
        ).scalar()
        if table_oids:
            assert oid == table_oids[-1]
        table_oids.append(oid)
        assert (
            await conn.execute(text("SELECT count(*) FROM organizations"))
        ).scalar() == 0
        await conn.execute(
            text(
                "INSERT INTO organizations (id, name) VALUES (gen_random_uuid(), 'test')"
            )
        )


@pytest.mark.recreate_schema
@pytest.mark.parametrize("iteration", range(2))
async def test_schema_changes_are_reset_between_marked_tests(db, iteration):
    async with db.begin() as conn:
        await conn.execute(text("SELECT name FROM organizations"))
        await conn.execute(
            text("ALTER TABLE organizations RENAME COLUMN name TO changed")
        )


async def test_schema_is_restored_after_marked_test(db):
    async with db.begin() as conn:
        assert (await conn.execute(text("SELECT name FROM organizations"))).all() == []
