"""0079 creates call_balance_holds and drops it on downgrade."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

pytestmark = pytest.mark.recreate_schema
spec = importlib.util.spec_from_file_location(
    "call_balance_holds_migration",
    Path(__file__).parents[1] / "migrations/versions/0079_call_balance_holds.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def test_call_balance_holds_migration_round_trip(db):
    async with db.begin() as conn:

        def apply(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.downgrade()
            migration.upgrade()

        await conn.run_sync(apply)
        columns = (
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'call_balance_holds' ORDER BY column_name"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert columns == [
            "call_id",
            "cents",
            "cents_per_call",
            "cents_per_minute",
            "created_at",
            "dials",
            "hold_until",
        ]
        delete_rule = (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'call_balance_holds'::regclass AND contype = 'f'"
                )
            )
        ).scalar_one()
        assert delete_rule in (b"c", "c")
