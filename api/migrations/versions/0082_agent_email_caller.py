"""Let a saved agent email the person on the call (``agents.email_caller_enabled``).

An agent setting, like ``voice_enabled`` or ``email_enabled``: a boolean
on ``agents``, off by default so existing agents do not change. Tables
checked: ``agents`` holds every per-agent setting, so the switch goes
there. The emails it sends are ordinary ``emails`` rows, and what happened
on the call is a ``call_events`` row; neither needs a new column.

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "email_caller_enabled",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "email_caller_enabled")
