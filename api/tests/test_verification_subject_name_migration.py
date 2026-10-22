"""0066: carrier_verifications gets subject_name, and the number live index
blocks only the same kind with the same name (any case)."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

spec = importlib.util.spec_from_file_location(
    "subject_name_migration",
    Path(__file__).parents[1] / "migrations/versions/0066_verification_subject_name.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


async def _insert(db, org, kind="person", name=None, with_name=True) -> None:
    columns = "organization_id, provider, country_code, number_type, subject_type, purpose, state, requirements_version"
    values = ":org, 'twilio', 'GB', 'mobile', :kind, 'number', 'approved', '1'"
    if with_name:
        columns += ", subject_name"
        values += ", :name"
    async with db.begin() as connection:
        await connection.execute(
            text(f"INSERT INTO carrier_verifications ({columns}) VALUES ({values})"),
            {"org": org, "kind": kind, "name": name},
        )


async def _index(db) -> str:
    async with db.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE indexname = 'carrier_verifications_number_live_uniq'"
                )
            )
        ).scalar_one()


async def _has_column(db) -> bool:
    async with db.connect() as connection:
        return bool(
            (
                await connection.execute(
                    text(
                        "SELECT 1 FROM information_schema.columns WHERE "
                        "table_name = 'carrier_verifications' "
                        "AND column_name = 'subject_name'"
                    )
                )
            ).first()
        )


async def test_down_then_up(db) -> None:
    # The test schema is the models' schema: already upgraded.
    await _run(db, migration.downgrade)
    assert not await _has_column(db)
    assert "lower" not in await _index(db)
    org = uuid.uuid4()
    await _insert(db, org, with_name=False)
    with pytest.raises(IntegrityError):
        await _insert(db, org, with_name=False)

    await _run(db, migration.upgrade)
    assert await _has_column(db)
    index = await _index(db)
    assert "lower(subject_name)" in index and "subject_name IS NOT NULL" in index
    # The row from before keeps no name; two people may now be live.
    await _insert(db, org, name="Ada Lovelace")
    await _insert(db, org, name="Grace Hopper")
    with pytest.raises(IntegrityError):
        await _insert(db, org, name="ADA LOVELACE")


async def test_downgrade_fails_while_two_of_one_kind_are_live(db) -> None:
    org = uuid.uuid4()
    await _insert(db, org, name="Ada Lovelace")
    await _insert(db, org, name="Grace Hopper")
    with pytest.raises(IntegrityError):
        await _run(db, migration.downgrade)
