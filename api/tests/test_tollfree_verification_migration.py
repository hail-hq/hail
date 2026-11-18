"""0078 upgrades/downgrades the existing verification table in place."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

pytestmark = pytest.mark.recreate_schema
spec = importlib.util.spec_from_file_location(
    "tollfree_migration",
    Path(__file__).parents[1]
    / "migrations/versions/0078_tollfree_number_verification.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def test_tollfree_migration_round_trip(db):
    async with db.begin() as conn:

        def apply(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.downgrade()
            migration.upgrade()

        await conn.run_sync(apply)
        indexes = (
            (
                await conn.execute(
                    text(
                        "SELECT indexdef FROM pg_indexes WHERE tablename='carrier_verifications'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            "carrier_verifications_tollfree_live_uniq" in value and "number_id" in value
            for value in indexes
        )
        assert any(
            "carrier_verifications_sms_live_uniq" in value
            and "number_type <> 'toll_free'" in value
            for value in indexes
        )
        constraint = (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint WHERE conname='carrier_verifications_number_id_fkey'"
                )
            )
        ).scalar_one()
        assert constraint == b"r" or constraint == "r"
