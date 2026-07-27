"""agent_send_log + platform_flags — velocity caps + kill switch for
agent-origin orgs (agent self-signup v1).

organizations.origin is added by hail-website's migration
(better-auth_migrations/2026-07-16T00-00-00.000Z.sql) — NOT here; that
table is website-owned. Deploy order: website migration first.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_send_log",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "agent_send_log_org_channel_created_idx",
        "agent_send_log",
        ["organization_id", "channel", "created_at"],
    )
    op.create_index(
        "agent_send_log_channel_created_idx",
        "agent_send_log",
        ["channel", "created_at"],
    )
    op.create_table(
        "platform_flags",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("platform_flags")
    op.drop_index("agent_send_log_channel_created_idx", table_name="agent_send_log")
    op.drop_index("agent_send_log_org_channel_created_idx", table_name="agent_send_log")
    op.drop_table("agent_send_log")
