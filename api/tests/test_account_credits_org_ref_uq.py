"""One ledger row per (organization, ref): the unique index and its migration."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

spec = importlib.util.spec_from_file_location(
    "account_credits_org_ref_uq_migration",
    Path(__file__).parents[1]
    / "migrations/versions/0072_account_credits_org_ref_uq.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

# The statement hail-website's ledger writers run (lib/account-credits.ts,
# lib/auth.ts). It only works while a unique index on exactly
# (organization_id, ref) exists.
WEBSITE_INSERT = """
INSERT INTO account_credits
  (organization_id, kind, channel, amount_cents, qty, ref, source, created_at)
VALUES (:org, 'debit', 'voice', -10, 1, :ref, 'usage', now())
ON CONFLICT (organization_id, ref) DO NOTHING
"""


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


async def _insert(db, org, ref) -> None:
    async with db.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO account_credits (organization_id, kind, channel, "
                "amount_cents, qty, ref, source) VALUES (:org, 'credit', "
                "'credit', 100, 1, :ref, 'test')"
            ),
            {"org": org, "ref": ref},
        )


async def _website_insert(db, org, ref) -> int:
    async with db.begin() as connection:
        result = await connection.execute(
            text(WEBSITE_INSERT), {"org": org, "ref": ref}
        )
        return result.rowcount


async def _count(db, org) -> int:
    async with db.begin() as connection:
        return (
            await connection.execute(
                text("SELECT count(*) FROM account_credits WHERE organization_id = :o"),
                {"o": org},
            )
        ).scalar_one()


async def test_same_ref_twice_is_refused(db) -> None:
    org = uuid.uuid4()
    await _insert(db, org, "stripe:pi_1")
    with pytest.raises(IntegrityError):
        await _insert(db, org, "stripe:pi_1")
    assert await _count(db, org) == 1


async def test_same_ref_in_another_org_and_null_refs_are_allowed(db) -> None:
    org, other = uuid.uuid4(), uuid.uuid4()
    await _insert(db, org, "stripe:pi_1")
    await _insert(db, other, "stripe:pi_1")
    await _insert(db, org, None)
    await _insert(db, org, None)
    assert await _count(db, org) == 3
    assert await _count(db, other) == 1


async def test_website_on_conflict_insert_skips_a_replay(db) -> None:
    org = uuid.uuid4()
    assert await _website_insert(db, org, "usage_event:1") == 1
    assert await _website_insert(db, org, "usage_event:1") == 0
    assert await _count(db, org) == 1


async def test_downgrade_allows_duplicates_and_upgrade_refuses_them(db) -> None:
    # The test schema is the models' schema: already upgraded.
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    await _insert(db, org, "dup")
    await _insert(db, org, "dup")
    # A database that already holds a duplicate stops the upgrade.
    with pytest.raises(IntegrityError):
        await _run(db, migration.upgrade)

    async with db.begin() as connection:
        await connection.execute(
            text("DELETE FROM account_credits WHERE organization_id = :o"),
            {"o": org},
        )
    await _run(db, migration.upgrade)
    await _insert(db, org, "dup")
    with pytest.raises(IntegrityError):
        await _insert(db, org, "dup")
