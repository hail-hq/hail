"""Let a saved agent email the person on the call (``agents.email_caller_enabled``).

An agent setting, like ``voice_enabled`` or ``email_enabled``: a boolean
on ``agents``, off by default so existing agents do not change. Tables
checked: ``agents`` holds every per-agent setting, so the switch goes
there. The emails it sends are ordinary ``emails`` rows (marked in
``metadata``), and what happened on the call is a ``call_events`` row;
neither needs a new column. A partial index on ``emails`` keeps the tool's
caps (per call, per caller number per day, per org per day) cheap. It is
built CONCURRENTLY so sends and inbound mail keep writing to ``emails``
while it builds; that cannot run in a transaction, hence the autocommit
block.

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
    with op.get_context().autocommit_block():
        op.create_index(
            "emails_caller_email_idx",
            "emails",
            ["organization_id", "requested_at"],
            postgresql_where=sa.text("(metadata ->> 'email_caller') = 'true'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "emails_caller_email_idx",
            table_name="emails",
            postgresql_concurrently=True,
        )
    op.drop_column("agents", "email_caller_enabled")
