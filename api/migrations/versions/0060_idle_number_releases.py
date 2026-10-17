"""Create idle_number_releases — warn-then-release state for idle numbers.

One row per dedicated number whose owners were warned that it had no calls
or texts for the idle window. Written and cleared by hail-website's daily
billing job: the row is deleted when the number sees activity again or once
it is released. ``release_after`` is fixed at warn time so the announced
date never moves.

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0060"
down_revision: str | None = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "idle_number_releases",
        sa.Column(
            "phone_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
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
    op.drop_table("idle_number_releases")
