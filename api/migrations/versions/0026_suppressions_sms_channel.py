"""Widen suppressions.channel CHECK to include 'sms'.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("suppressions_channel_check", "suppressions", type_="check")
    op.create_check_constraint(
        "suppressions_channel_check",
        "suppressions",
        "channel IN ('voice','email','sms','all')",
    )


def downgrade() -> None:
    op.drop_constraint("suppressions_channel_check", "suppressions", type_="check")
    op.create_check_constraint(
        "suppressions_channel_check",
        "suppressions",
        "channel IN ('voice','email','all')",
    )
