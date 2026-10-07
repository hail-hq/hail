"""Thread lookup indexes on calls and sms.

A thread is (organization_id, agent_id, caller number). The caller is in
from_e164 on inbound rows and to_e164 on outbound rows, so each table gets
one index per column. Built CONCURRENTLY so writes to calls and sms do not
block; that cannot run in a transaction, hence the autocommit block.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "calls_thread_from_idx",
            "calls",
            ["organization_id", "agent_id", "from_e164"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "calls_thread_to_idx",
            "calls",
            ["organization_id", "agent_id", "to_e164"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "sms_thread_from_idx",
            "sms",
            ["organization_id", "agent_id", "from_e164", "requested_at"],
            postgresql_concurrently=True,
        )
        op.create_index(
            "sms_thread_to_idx",
            "sms",
            ["organization_id", "agent_id", "to_e164", "requested_at"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table in (
            ("sms_thread_to_idx", "sms"),
            ("sms_thread_from_idx", "sms"),
            ("calls_thread_to_idx", "calls"),
            ("calls_thread_from_idx", "calls"),
        ):
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
