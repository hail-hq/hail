import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

spec = importlib.util.spec_from_file_location(
    "number_order_migration",
    Path(__file__).parents[1] / "migrations/versions/0048_number_order_state.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def test_additive_migration_upgrades_original_and_revised_0047(db, monkeypatch):
    async with db.begin() as connection:
        await connection.execute(
            text(
                "ALTER TABLE phone_numbers ALTER COLUMN provider_resource_id SET NOT NULL"
            )
        )
        await connection.execute(text("DROP INDEX phone_numbers_e164_live_uniq"))
        await connection.execute(
            text(
                "CREATE UNIQUE INDEX phone_numbers_e164_live_uniq ON phone_numbers (e164) WHERE provisioning_state <> 'released'"
            )
        )

        def upgrade(sync):
            monkeypatch.setattr(
                migration, "op", Operations(MigrationContext.configure(sync))
            )
            migration.upgrade()

        await connection.run_sync(upgrade)
        # An environment that already ran the edited 0047 is safe too.
        await connection.run_sync(upgrade)
        nullable = (
            await connection.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns WHERE table_name='phone_numbers' AND column_name='provider_resource_id'"
                )
            )
        ).scalar_one()
        index = (
            await connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname='phone_numbers_e164_live_uniq'"
                )
            )
        ).scalar_one()
        assert nullable == "YES"
        assert "failed" in index and "released" in index
