"""sms_events table — append-only SMS lifecycle events.

Mirrors ``email_events``' shape (denormalized ``organization_id`` for
join-free org-wide streaming, dedup unique constraint sized for
at-least-once provider status callbacks). Written today by the SMS route
on status transitions; the planned Twilio delivery-status webhook appends
to the same table.

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sms_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "sms_id",
            UUID(as_uuid=True),
            sa.ForeignKey("sms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "sms_id", "kind", "occurred_at", name="sms_events_dedup_uq"
        ),
    )
    op.create_index(
        "sms_events_sms_occurred_idx", "sms_events", ["sms_id", "occurred_at"]
    )
    op.create_index(
        "sms_events_org_occurred_kind_idx",
        "sms_events",
        ["organization_id", "occurred_at", "kind"],
    )


def downgrade() -> None:
    op.drop_index("sms_events_org_occurred_kind_idx", table_name="sms_events")
    op.drop_index("sms_events_sms_occurred_idx", table_name="sms_events")
    op.drop_table("sms_events")
