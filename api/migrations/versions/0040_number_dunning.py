"""Create number_dunning — per-org warn-then-release state for unfunded numbers.

One row per org that holds active dedicated numbers on a zero/negative
balance and has been warned by email. Written and cleared by hail-website's
dunning job; ``release_after`` (warned_at + grace) is fixed at warn time so
the announced deadline never moves.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "number_dunning",
        sa.Column("organization_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("warned_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("release_after", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("number_dunning")
