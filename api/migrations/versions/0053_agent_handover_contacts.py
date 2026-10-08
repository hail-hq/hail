"""Contacts an agent may hand a live call over to, and the call limit after
the contact answers.

Revision ID: 0053
Revises: 0052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("handover_max_duration_seconds", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "agents_handover_max_duration_check",
        "agents",
        "handover_max_duration_seconds IS NULL "
        "OR handover_max_duration_seconds BETWEEN 60 AND 3600",
    )
    op.create_table(
        "agent_handover_contacts",
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "contact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("contacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(note) BETWEEN 1 AND 200", name="agent_handover_note_len"
        ),
    )
    op.create_index(
        "agent_handover_contacts_contact_idx",
        "agent_handover_contacts",
        ["contact_id"],
    )


def downgrade() -> None:
    op.drop_index("agent_handover_contacts_contact_idx")
    op.drop_table("agent_handover_contacts")
    op.drop_constraint("agents_handover_max_duration_check", "agents")
    op.drop_column("agents", "handover_max_duration_seconds")
