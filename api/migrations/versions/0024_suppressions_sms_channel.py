"""Widen suppressions.channel CHECK to include 'sms'.

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
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
