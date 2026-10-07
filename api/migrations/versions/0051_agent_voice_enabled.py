"""Agents answer calls, texts, or both: ``agents.voice_enabled``.

Mirrors ``sms_enabled`` (0050). A number can route a channel only to an
agent that answers it; an inbound call to an agent with voice off fails
with ``no_agent``.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "voice_enabled",
            sa.Boolean(),
            server_default=sa.text("TRUE"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "voice_enabled")
