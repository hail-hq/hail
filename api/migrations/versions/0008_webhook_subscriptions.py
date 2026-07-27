"""webhook subscriptions + deliveries

Phase 5 of the inbound-email milestone. The org-wide event firehose
(``webhook_subscriptions``) and the per-attempt audit / retry queue
(``webhook_deliveries``) are mounted in lockstep — every active
subscription generates delivery rows that the background worker drains
with the Stripe-style HMAC envelope.

A delivery is owned by either an org-wide subscription **or** a
per-domain webhook (``email_domains.webhook_url``), not both. The
``webhook_deliveries_target_check`` enforces that disjunction; the same
table backs both paths so retry / replay / observability stay uniform.

See docs/superpowers/specs/2026-06-06-inbound-email-design.md §3.5.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_url", sa.Text(), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("event_types", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_failure_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "webhook_subscriptions_status_check",
        "webhook_subscriptions",
        "status IN ('active','disabled')",
    )
    op.create_check_constraint(
        "webhook_subscriptions_event_types_nonempty",
        "webhook_subscriptions",
        "cardinality(event_types) >= 1",
    )
    op.create_index(
        "webhook_subscriptions_org_idx",
        "webhook_subscriptions",
        ["organization_id"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "email_domain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_domains.id", ondelete="CASCADE"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("response_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column(
            "next_attempt_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("succeeded_at", sa.TIMESTAMP(timezone=True)),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "webhook_deliveries_target_check",
        "webhook_deliveries",
        "subscription_id IS NOT NULL OR email_domain_id IS NOT NULL",
    )
    op.create_check_constraint(
        "webhook_deliveries_status_check",
        "webhook_deliveries",
        "status IN ('pending','succeeded','failed','dead')",
    )
    op.create_index(
        "webhook_deliveries_pending_idx",
        "webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("webhook_deliveries_pending_idx", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("webhook_subscriptions_org_idx", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
