"""Thread lookup indexes on calls and sms.

A thread is (organization_id, agent_id, caller number). The caller is in
from_e164 on inbound rows and to_e164 on outbound rows, so each table gets
one index per column.

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
    op.create_index(
        "calls_thread_from_idx", "calls", ["organization_id", "agent_id", "from_e164"]
    )
    op.create_index(
        "calls_thread_to_idx", "calls", ["organization_id", "agent_id", "to_e164"]
    )
    op.create_index(
        "sms_thread_from_idx",
        "sms",
        ["organization_id", "agent_id", "from_e164", "requested_at"],
    )
    op.create_index(
        "sms_thread_to_idx",
        "sms",
        ["organization_id", "agent_id", "to_e164", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("sms_thread_to_idx", table_name="sms")
    op.drop_index("sms_thread_from_idx", table_name="sms")
    op.drop_index("calls_thread_to_idx", table_name="calls")
    op.drop_index("calls_thread_from_idx", table_name="calls")
