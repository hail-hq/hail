"""Add the answering-machine-detection values to the call_end_reason ENUM.

`voicemail_reached` and `machine_unavailable` are stamped by the voicebot when
AMD decides a machine picked up; both map to status='no_answer'.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction, hence the
autocommit block (same shape as 0024).

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'voicemail_reached'"
        )
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'machine_unavailable'"
        )


def downgrade() -> None:
    # Postgres cannot drop a single ENUM value; leaving it is harmless.
    pass
