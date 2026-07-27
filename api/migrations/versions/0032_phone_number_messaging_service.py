"""Add messaging_service_sid to phone_numbers.

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "phone_numbers", sa.Column("messaging_service_sid", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("phone_numbers", "messaging_service_sid")
