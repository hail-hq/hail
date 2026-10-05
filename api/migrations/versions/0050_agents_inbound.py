"""Agents, number routing, inbound calls and texts.

- ``agents``: a saved brain (instructions, greeting, AI line, voice, tools,
  limits). Numbers point at one for calls and one for texts.
- ``phone_numbers``: ``voice_agent_id``, ``sms_agent_id``,
  ``inbound_registered_at`` (set while the number is on its carrier's
  LiveKit inbound trunk and attached for inbound at the carrier).
- ``calls``: ``from_number_id`` becomes nullable (inbound callers have no
  row), ``to_number_id`` (the dialed org number), ``agent_id``, and a CHECK
  that each direction carries its number.
- ``sms``: ``agent_id`` (the agent that wrote an outbound reply) and
  ``agent_reply_state`` (with ``agent_reply_attempts`` and
  ``agent_reply_available_at``: retry count, backoff time and claim lease) on
  inbound rows routed to a text agent.
- ``organization_call_settings``: ``max_duration_seconds`` becomes nullable
  (NULL = service default) and gains ``ai_disclosure_line``.
- ``call_end_reason``: ``insufficient_funds``, ``no_agent`` (inbound calls
  Hail refused before answering). ``ALTER TYPE ... ADD VALUE`` cannot run in
  a transaction, hence the autocommit block (same shape as 0046).

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("first_message", sa.Text(), nullable=True),
        sa.Column(
            "ai_disclosure",
            sa.Boolean(),
            server_default=sa.text("TRUE"),
            nullable=False,
        ),
        sa.Column("ai_disclosure_line", sa.Text(), nullable=True),
        sa.Column(
            "voice_config", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("tools", ARRAY(sa.Text()), nullable=True),
        sa.Column("max_duration_seconds", sa.Integer(), nullable=True),
        sa.Column(
            "sms_enabled", sa.Boolean(), server_default=sa.text("TRUE"), nullable=False
        ),
        sa.Column("status", sa.Text(), server_default="live", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", "name", name="agents_org_name_uq"),
        sa.CheckConstraint("status IN ('live','paused')", name="agents_status_check"),
        sa.CheckConstraint(
            "max_duration_seconds IS NULL OR max_duration_seconds BETWEEN 60 AND 3600",
            name="agents_max_duration_check",
        ),
    )
    # No separate organization_id index: agents_org_name_uq leads with it.

    op.add_column(
        "phone_numbers",
        sa.Column(
            "voice_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "phone_numbers",
        sa.Column(
            "sms_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "phone_numbers",
        sa.Column("inbound_registered_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.alter_column("calls", "from_number_id", nullable=True)
    op.add_column(
        "calls",
        sa.Column(
            "to_number_id",
            UUID(as_uuid=True),
            sa.ForeignKey("phone_numbers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "calls",
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "calls_number_for_direction",
        "calls",
        "(direction = 'outbound' AND from_number_id IS NOT NULL)"
        " OR (direction = 'inbound' AND to_number_id IS NOT NULL)",
    )

    op.add_column(
        "sms",
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("sms", sa.Column("agent_reply_state", sa.Text(), nullable=True))
    op.add_column(
        "sms",
        sa.Column(
            "agent_reply_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "sms",
        sa.Column("agent_reply_available_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_check_constraint(
        "sms_agent_reply_state_check",
        "sms",
        "agent_reply_state IS NULL OR agent_reply_state IN "
        "('pending','processing','done','skipped','failed')",
    )
    # The text worker claims pending and expired-lease rows; keep that scan cheap.
    op.create_index(
        "sms_agent_reply_pending_idx",
        "sms",
        ["requested_at"],
        postgresql_where=sa.text("agent_reply_state IN ('pending','processing')"),
    )

    op.alter_column("organization_call_settings", "max_duration_seconds", nullable=True)
    op.add_column(
        "organization_call_settings",
        sa.Column("ai_disclosure_line", sa.Text(), nullable=True),
    )

    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'insufficient_funds'"
        )
        op.execute("ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'no_agent'")


def downgrade() -> None:
    # Inbound calls have no from_number_id. Never delete call history here.
    orphans = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM calls WHERE from_number_id IS NULL"))
        .scalar_one()
    )
    if orphans:
        raise RuntimeError(
            f"Cannot downgrade 0050: {orphans} call(s) have from_number_id IS NULL "
            "(inbound calls). Export or remove them by hand, then rerun."
        )
    op.drop_column("organization_call_settings", "ai_disclosure_line")
    op.execute(
        "UPDATE organization_call_settings SET max_duration_seconds = 300"
        " WHERE max_duration_seconds IS NULL"
    )
    op.alter_column(
        "organization_call_settings", "max_duration_seconds", nullable=False
    )
    op.drop_index("sms_agent_reply_pending_idx", table_name="sms")
    op.drop_constraint("sms_agent_reply_state_check", "sms", type_="check")
    op.drop_column("sms", "agent_reply_available_at")
    op.drop_column("sms", "agent_reply_attempts")
    op.drop_column("sms", "agent_reply_state")
    op.drop_column("sms", "agent_id")
    op.drop_constraint("calls_number_for_direction", "calls", type_="check")
    op.drop_column("calls", "agent_id")
    op.drop_column("calls", "to_number_id")
    op.alter_column("calls", "from_number_id", nullable=False)
    op.drop_column("phone_numbers", "inbound_registered_at")
    op.drop_column("phone_numbers", "sms_agent_id")
    op.drop_column("phone_numbers", "voice_agent_id")
    op.drop_table("agents")
    # Postgres cannot drop a single ENUM value; leaving them is harmless.
