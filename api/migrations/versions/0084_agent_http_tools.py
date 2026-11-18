"""Customer HTTP tools on agents (``agent_http_tools``) and the secret that
signs their requests (``agents.http_tool_secret_encrypted``).

An HTTP tool is a customer API an agent may call mid-conversation: name,
description and JSON-schema parameters for the model, the URL, static
headers (Fernet ciphertext) and a timeout. An agent holds a list of them,
like its handover contacts.

Tables checked:

* ``agents``: one row per agent. ``agents.tools`` names built-in tools only
  (a text array); a list of structured tools with encrypted headers does not
  fit in it. The per-agent signing secret does fit: it is agent config, one
  value per agent, so it is a column there.
* ``agent_handover_contacts``: people the agent dials, keyed by contact or
  member. A tool is not a person.
* ``webhook_subscriptions``: org-wide event subscriptions with their own
  delivery rows and retry ladder. A tool call is one synchronous request
  per model turn, owned by an agent, with no deliveries or retries.
* ``org_provider_config``: one BYO provider key per pipeline layer and
  org. Not a list per agent.

None holds this kind of record, so it gets its own table, linked to
``agents`` with ON DELETE CASCADE.

Revision ID: 0084
Revises: 0083
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0084"
down_revision: str | None = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("http_tool_secret_encrypted", sa.Text(), nullable=True),
    )
    op.create_table(
        "agent_http_tools",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("headers_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "header_names",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("timeout_seconds", sa.SmallInteger(), nullable=False),
        sa.Column("wait_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("agent_id", "name", name="agent_http_tools_agent_name_uq"),
        sa.CheckConstraint(
            "timeout_seconds BETWEEN 1 AND 10", name="agent_http_tools_timeout_check"
        ),
        sa.CheckConstraint("url LIKE 'https://%'", name="agent_http_tools_https_check"),
    )


def downgrade() -> None:
    op.drop_table("agent_http_tools")
    op.drop_column("agents", "http_tool_secret_encrypted")
