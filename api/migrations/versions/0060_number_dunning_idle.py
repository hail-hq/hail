"""Extend number_dunning to hold per-number idle warnings.

number_dunning held one row per org warned for a low balance. hail-website's
daily billing job now also warns about dedicated numbers with no calls or
texts for the idle window, one row per number. Both are "this will be
released on release_after" warnings, so they share the table:

- ``reason``: ``balance`` (existing rows, one per org) or ``idle``.
- ``phone_number_id``: NULL for ``balance`` (the whole org), the number for
  ``idle``.
- ``id`` becomes the primary key; uniqueness moves to two partial indexes.

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
    op.add_column(
        "number_dunning",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
    )
    op.add_column(
        "number_dunning",
        sa.Column("reason", sa.Text(), server_default="balance", nullable=False),
    )
    op.add_column(
        "number_dunning",
        sa.Column(
            "phone_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.drop_constraint("number_dunning_pkey", "number_dunning", type_="primary")
    op.create_primary_key("number_dunning_pkey", "number_dunning", ["id"])
    op.create_check_constraint(
        "number_dunning_reason",
        "number_dunning",
        "(reason = 'balance' AND phone_number_id IS NULL)"
        " OR (reason = 'idle' AND phone_number_id IS NOT NULL)",
    )
    op.create_index(
        "number_dunning_balance_org_key",
        "number_dunning",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("reason = 'balance'"),
    )
    op.create_index(
        "number_dunning_idle_number_key",
        "number_dunning",
        ["phone_number_id"],
        unique=True,
        postgresql_where=sa.text("reason = 'idle'"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM number_dunning WHERE reason <> 'balance'")
    op.drop_index("number_dunning_idle_number_key", table_name="number_dunning")
    op.drop_index("number_dunning_balance_org_key", table_name="number_dunning")
    op.drop_constraint("number_dunning_reason", "number_dunning", type_="check")
    op.drop_constraint("number_dunning_pkey", "number_dunning", type_="primary")
    op.create_primary_key("number_dunning_pkey", "number_dunning", ["organization_id"])
    op.drop_column("number_dunning", "phone_number_id")
    op.drop_column("number_dunning", "reason")
    op.drop_column("number_dunning", "id")
