"""0092 creates sip_connections and the pbx number, leg and extension columns."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

pytestmark = pytest.mark.recreate_schema
spec = importlib.util.spec_from_file_location(
    "sip_connections_migration",
    Path(__file__).parents[1] / "migrations/versions/0092_sip_connections.py",
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


async def test_sip_connections_migration_round_trip(db):
    async with db.begin() as conn:

        def apply(sync):
            migration.op = Operations(MigrationContext.configure(sync))
            migration.downgrade()
            migration.upgrade()

        await conn.run_sync(apply)

        async def columns(table):
            rows = await conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t ORDER BY column_name"
                ),
                {"t": table},
            )
            return rows.scalars().all()

        assert await columns("sip_connections") == [
            "created_at",
            "default_region",
            "deleted_at",
            "encrypted_inbound_password",
            "encrypted_pbx_password",
            "id",
            "inbound_username",
            "label",
            "livekit_inbound_trunk_id",
            "livekit_outbound_trunk_id",
            "organization_id",
            "pbx_host",
            "pbx_port",
            "pbx_transport",
            "pbx_username",
            "updated_at",
        ]
        assert "sip_connection_id" in await columns("phone_numbers")
        assert "extension" in await columns("contacts")
        delete_rule = (
            await conn.execute(
                text(
                    "SELECT confdeltype FROM pg_constraint "
                    "WHERE conrelid = 'phone_numbers'::regclass AND contype = 'f' "
                    "AND confrelid = 'sip_connections'::regclass"
                )
            )
        ).scalar_one()
        assert delete_rule in (b"r", "r")
        names = (
            (
                await conn.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conname IN ("
                        "'phone_numbers_state_check',"
                        "'phone_numbers_pbx_connection_check',"
                        "'call_legs_provider_check',"
                        "'contacts_phone_email_or_extension',"
                        "'contacts_extension_format',"
                        "'sip_connections_label_len',"
                        "'sip_connections_transport_check',"
                        "'sip_connections_port_check',"
                        "'sip_connections_inbound_username_key')"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(names) == 9
        old = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'contacts_phone_or_email'"
                )
            )
        ).scalar_one()
        assert old == 0
