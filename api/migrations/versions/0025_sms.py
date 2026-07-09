"""sms table — outbound (and later inbound) text messages.

One row per message, mirroring ``calls``' shape (single plain-text
``status`` column, not an ENUM — SMS has no multi-valued end-reason
analog). ``provider_message_sid`` is unique but nullable until the
provider call returns.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "from_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id"),
            nullable=False,
        ),
        sa.Column("from_e164", sa.Text(), nullable=False),
        sa.Column("to_e164", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), server_default="outbound", nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), server_default="twilio", nullable=False),
        sa.Column("provider_message_sid", sa.Text(), nullable=True, unique=True),
        sa.Column("segment_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('outbound','inbound')",
            name="sms_direction_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued','sent','delivered','failed','undelivered','received')",
            name="sms_status_check",
        ),
    )
    # Composites mirror calls' (0001): the list endpoint keyset-paginates on
    # (created_at DESC, id DESC) per org, with an optional to_e164 filter.
    op.create_index(
        "idx_sms_org_created", "sms", ["organization_id", sa.text("created_at DESC")]
    )
    op.create_index("idx_sms_to", "sms", ["organization_id", "to_e164"])


def downgrade() -> None:
    op.drop_index("idx_sms_to", table_name="sms")
    op.drop_index("idx_sms_org_created", table_name="sms")
    op.drop_table("sms")
