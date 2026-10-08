"""Handover to team members: a handover row points at a manual contact OR an
org member.

``user_id`` carries no FK: ``users`` is website-owned (see 0001/0029). The
composite PK (agent_id, contact_id) becomes a surrogate ``id``, with one
partial unique index per kind. Existing rows keep their contact and get an id.

Revision ID: 0054
Revises: 0053
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels = None
depends_on = None

_T = "agent_handover_contacts"


def upgrade() -> None:
    op.add_column(
        _T,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.drop_constraint("agent_handover_contacts_pkey", _T, type_="primary")
    op.create_primary_key("agent_handover_contacts_pkey", _T, ["id"])
    op.alter_column(_T, "contact_id", nullable=True)
    op.add_column(_T, sa.Column("user_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint(
        "agent_handover_one_target", _T, "num_nonnulls(contact_id, user_id) = 1"
    )
    op.create_index("agent_handover_contacts_agent_idx", _T, ["agent_id"])
    op.create_index(
        "agent_handover_contacts_agent_contact_uq",
        _T,
        ["agent_id", "contact_id"],
        unique=True,
        postgresql_where=sa.text("contact_id IS NOT NULL"),
    )
    op.create_index(
        "agent_handover_contacts_agent_user_uq",
        _T,
        ["agent_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM {_T} WHERE user_id IS NOT NULL")
    op.drop_index("agent_handover_contacts_agent_user_uq", _T)
    op.drop_index("agent_handover_contacts_agent_contact_uq", _T)
    op.drop_index("agent_handover_contacts_agent_idx", _T)
    op.drop_constraint("agent_handover_one_target", _T, type_="check")
    op.drop_column(_T, "user_id")
    op.alter_column(_T, "contact_id", nullable=False)
    op.drop_constraint("agent_handover_contacts_pkey", _T, type_="primary")
    op.drop_column(_T, "id")
    op.create_primary_key(
        "agent_handover_contacts_pkey", _T, ["agent_id", "contact_id"]
    )
