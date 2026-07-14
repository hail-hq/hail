"""sms.to_number_id + nullable from_number_id.

Inbound rows have no sending PhoneNumber (the sender is external), so
from_number_id becomes nullable and the org's receiving number is recorded
in the new to_number_id FK. Outbound rows are unchanged (from_number_id set,
to_number_id NULL). No backfill: existing outbound rows keep from_number_id
and leave to_number_id NULL.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sms",
        sa.Column(
            "to_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id"),
            nullable=True,
        ),
    )
    op.alter_column(
        "sms", "from_number_id", existing_type=UUID(as_uuid=True), nullable=True
    )


def downgrade() -> None:
    # Restoring NOT NULL is safe only while no inbound (NULL from_number_id) rows exist.
    op.alter_column(
        "sms", "from_number_id", existing_type=UUID(as_uuid=True), nullable=False
    )
    op.drop_column("sms", "to_number_id")
