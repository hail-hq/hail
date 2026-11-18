"""Agents finish each reply before listening again: ``agents.steady_speech``.

A per-agent call setting, next to ``ai_disclosure`` on ``agents`` (the table
that already holds each agent's call settings as columns). Existing agents keep
today's behavior (FALSE); agents created from now on default to TRUE.

Revision ID: 0083
Revises: 0082
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "steady_speech",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )
    op.alter_column("agents", "steady_speech", server_default=sa.text("TRUE"))


def downgrade() -> None:
    op.drop_column("agents", "steady_speech")
