"""email_events table + allow 'delivered' email status.

One append-only row per SES lifecycle event (and a synthetic ``sent`` row
written at send time). The dedup unique index absorbs SNS at-least-once
redelivery. ``organization_id`` is denormalized so account-level stats
aggregate without a join. The emails CHECK constraint gains 'delivered'
(Postgres can't widen a CHECK in place — drop and re-add).

Downgrade refuses to run while any email has status='delivered', since the
narrower constraint doesn't allow that value and re-adding it would fail
opaquely (or require silently remapping data).

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "email_id",
            UUID(as_uuid=True),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "email_id", "kind", "occurred_at", name="email_events_dedup_uq"
        ),
    )
    op.create_index(
        "email_events_email_occurred_idx", "email_events", ["email_id", "occurred_at"]
    )
    op.create_index(
        "email_events_org_occurred_kind_idx",
        "email_events",
        ["organization_id", "occurred_at", "kind"],
    )
    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','delivered','failed','bounced',"
        "'complained','received')",
    )


def downgrade() -> None:
    delivered = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM emails WHERE status = 'delivered'"))
        .scalar_one()
    )
    if delivered:
        raise RuntimeError(
            f"cannot downgrade: {delivered} emails rows have status='delivered', "
            "which the narrower emails_status_check does not allow; remap them "
            "first (e.g. UPDATE emails SET status='sent' WHERE "
            "status='delivered') if that's an acceptable approximation for "
            "your data, then re-run the downgrade"
        )
    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained','received')",
    )
    op.drop_index("email_events_org_occurred_kind_idx", table_name="email_events")
    op.drop_index("email_events_email_occurred_idx", table_name="email_events")
    op.drop_table("email_events")
