"""0067: one live number verification with no name per subject type."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

spec = importlib.util.spec_from_file_location(
    "nameless_migration",
    Path(__file__).parents[1]
    / "migrations/versions/0067_verification_nameless_uniq.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def _run(db, step) -> None:
    async with db.begin() as connection:

        def go(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            step()

        await connection.run_sync(go)


async def _insert(db, org, kind="person", state="approved", name=None) -> None:
    async with db.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO carrier_verifications (organization_id, provider, "
                "country_code, number_type, subject_type, purpose, state, "
                "requirements_version, subject_name) VALUES (:org, 'twilio', "
                "'GB', 'mobile', :kind, 'number', :state, '1', :name)"
            ),
            {"org": org, "kind": kind, "state": state, "name": name},
        )


async def test_down_then_up(db) -> None:
    # The test schema is the models' schema: already upgraded.
    await _run(db, migration.downgrade)
    org = uuid.uuid4()
    await _insert(db, org)
    await _insert(db, org)  # no index: two nameless rows of one kind

    other = uuid.uuid4()
    with pytest.raises(IntegrityError):
        await _run(db, migration.upgrade)  # the duplicate above blocks it
    async with db.begin() as connection:
        await connection.execute(
            text("DELETE FROM carrier_verifications WHERE organization_id = :org"),
            {"org": org},
        )
    await _run(db, migration.upgrade)
    await _insert(db, other)
    await _insert(db, other, kind="business")
    await _insert(db, other, state="rejected")
    await _insert(db, other, name="Ada Lovelace")
    with pytest.raises(IntegrityError):
        await _insert(db, other, state="submitted")
