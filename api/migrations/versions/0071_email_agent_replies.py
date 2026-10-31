"""Email agent replies: route an address to an agent.

1. ``email_domains.email_agent_id`` names the agent that answers mail to the
   address, and ``email_domains.email_agent_scope`` says who it answers:
   ``members`` (default) or ``anyone``.
2. ``agents.email_enabled`` says whether the agent answers email.
3. ``emails`` gets ``agent_id`` and the reply columns ``sms`` has:
   ``agent_reply_state``, ``agent_reply_attempts``, ``agent_reply_available_at``.
4. ``emails.from_verified`` is true only when ingest saw one ``From`` address and
   a DMARC ``PASS``. It is an allowlist: older rows stay false, so they stay out
   of an agent's chat.

Tables checked (no new table): ``email_domains`` routes an address the way
``phone_numbers`` routes a number. ``agents`` holds the per-channel switches.
``sms`` holds texts only, and an email's reply state belongs to its inbound
row, as a text's does.

Downgrade drops the columns, and with them each address's agent.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "email_enabled",
            sa.Boolean(),
            server_default=sa.text("TRUE"),
            nullable=False,
        ),
    )
    op.add_column(
        "email_domains",
        sa.Column(
            "email_agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "email_domains",
        sa.Column(
            "email_agent_scope",
            sa.Text(),
            server_default="members",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "email_domains_email_agent_scope_check",
        "email_domains",
        "email_agent_scope IN ('members','anyone')",
    )
    op.add_column(
        "emails",
        sa.Column(
            "agent_id",
            UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "emails",
        sa.Column(
            "from_verified",
            sa.Boolean(),
            server_default=sa.text("FALSE"),
            nullable=False,
        ),
    )
    op.add_column("emails", sa.Column("agent_reply_state", sa.Text(), nullable=True))
    op.add_column(
        "emails",
        sa.Column(
            "agent_reply_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "emails",
        sa.Column("agent_reply_available_at", sa.TIMESTAMP(timezone=True)),
    )
    op.create_check_constraint(
        "emails_agent_reply_state_check",
        "emails",
        "agent_reply_state IS NULL OR agent_reply_state IN "
        "('pending','processing','done','skipped','failed')",
    )
    # The worker claims pending and expired-lease rows; keep that scan cheap.
    op.create_index(
        "emails_agent_reply_pending_idx",
        "emails",
        ["requested_at"],
        postgresql_where=sa.text("agent_reply_state IN ('pending','processing')"),
    )
    # The thread cap counts one agent's recent replies.
    op.create_index(
        "emails_agent_thread_idx",
        "emails",
        ["organization_id", "agent_id", "requested_at"],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("emails_agent_thread_idx", table_name="emails")
    op.drop_index("emails_agent_reply_pending_idx", table_name="emails")
    op.drop_constraint("emails_agent_reply_state_check", "emails", type_="check")
    op.drop_column("emails", "agent_reply_available_at")
    op.drop_column("emails", "agent_reply_attempts")
    op.drop_column("emails", "agent_reply_state")
    op.drop_column("emails", "from_verified")
    op.drop_column("emails", "agent_id")
    op.drop_constraint(
        "email_domains_email_agent_scope_check", "email_domains", type_="check"
    )
    op.drop_column("email_domains", "email_agent_scope")
    op.drop_column("email_domains", "email_agent_id")
    op.drop_column("agents", "email_enabled")
