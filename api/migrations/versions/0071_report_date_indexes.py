"""Date indexes on calls, sms, emails and audit_log.

The daily activity report reads each table by date, across organizations.
The existing date indexes start with organization_id, so that read scans the
whole table. Built CONCURRENTLY so writes do not block; that cannot run in a
transaction, hence the autocommit block.

Revision ID: 0071
Revises: 0070
"""

from __future__ import annotations

from alembic import op

revision: str = "0071"
down_revision: str | None = "0070"
branch_labels = None
depends_on = None

INDEXES = (
    ("calls_requested_at_idx", "calls", "requested_at"),
    ("sms_requested_at_idx", "sms", "requested_at"),
    ("emails_requested_at_idx", "emails", "requested_at"),
    ("audit_log_occurred_at_idx", "audit_log", "occurred_at"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, column in INDEXES:
            op.create_index(name, table, [column], postgresql_concurrently=True)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, _ in INDEXES:
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
