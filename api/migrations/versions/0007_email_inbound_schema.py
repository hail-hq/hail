"""inbound email schema: direction, attachments, action columns

Adds the schema additions for the inbound-email milestone:

* Email.direction column (defaults 'outbound'; new inbound rows write 'inbound')
* Email inbound metadata columns (message_id, in_reply_to, references_ids,
  raw_s3_key, provider_received_at, spam/virus/spf/dkim/dmarc verdicts)
* emails.email_domain_id becomes nullable (custom-domain inbound in the
  next milestone won't always map to a known domain row); a CHECK
  ensures outbound rows always carry one.
* emails.status expands to include 'received'.
* Partial unique index on (organization_id, message_id) for inbound rows —
  source of truth for ingest idempotency (SES re-deliveries short-circuit).
* email_domains gets inbound action columns (inbound_enabled, forward_to,
  webhook_url, webhook_secret_encrypted, forward_rate_per_hour) with paired
  CHECKs ensuring an enabled domain has at least one action and the
  webhook URL/secret pair stay in sync.
* email_attachments table for MIME attachments — one row per attachment,
  S3 key stored, cascade-on-email-delete.

See docs/superpowers/specs/2026-06-06-inbound-email-design.md.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-06
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- emails: direction + inbound columns ----
    op.add_column(
        "emails",
        sa.Column("direction", sa.Text(), nullable=False, server_default="outbound"),
    )
    op.create_check_constraint(
        "emails_direction_check", "emails", "direction IN ('outbound','inbound')"
    )
    op.alter_column("emails", "email_domain_id", nullable=True)
    op.create_check_constraint(
        "emails_outbound_has_domain",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL",
    )
    op.add_column(
        "emails", sa.Column("provider_received_at", sa.TIMESTAMP(timezone=True))
    )
    op.add_column("emails", sa.Column("message_id", sa.Text()))
    op.add_column("emails", sa.Column("in_reply_to", sa.Text()))
    op.add_column("emails", sa.Column("references_ids", postgresql.ARRAY(sa.Text())))
    op.add_column("emails", sa.Column("raw_s3_key", sa.Text()))
    op.add_column("emails", sa.Column("spam_verdict", sa.Text()))
    op.add_column("emails", sa.Column("virus_verdict", sa.Text()))
    op.add_column("emails", sa.Column("dkim_verdict", sa.Text()))
    op.add_column("emails", sa.Column("spf_verdict", sa.Text()))
    op.add_column("emails", sa.Column("dmarc_verdict", sa.Text()))

    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained','received')",
    )

    op.create_index(
        "emails_org_direction_created_idx",
        "emails",
        ["organization_id", "direction", sa.text("created_at DESC")],
    )
    op.create_index("emails_message_id_idx", "emails", ["message_id"])
    op.create_index(
        "emails_inbound_message_id_uq",
        "emails",
        ["organization_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("direction = 'inbound' AND message_id IS NOT NULL"),
    )

    # ---- email_domains: per-domain inbound action columns ----
    op.add_column(
        "email_domains",
        sa.Column(
            "inbound_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("email_domains", sa.Column("forward_to", postgresql.ARRAY(sa.Text())))
    op.add_column("email_domains", sa.Column("webhook_url", sa.Text()))
    op.add_column("email_domains", sa.Column("webhook_secret_encrypted", sa.Text()))
    op.add_column("email_domains", sa.Column("forward_rate_per_hour", sa.Integer()))
    op.create_check_constraint(
        "email_domains_inbound_action",
        "email_domains",
        "NOT inbound_enabled OR forward_to IS NOT NULL OR webhook_url IS NOT NULL",
    )
    op.create_check_constraint(
        "email_domains_webhook_pair",
        "email_domains",
        "(webhook_url IS NULL) = (webhook_secret_encrypted IS NULL)",
    )

    # ---- email_attachments ----
    op.create_table(
        "email_attachments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "email_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("content_id", sa.Text()),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("email_attachments_email_id_idx", "email_attachments", ["email_id"])


def downgrade() -> None:
    op.drop_index("email_attachments_email_id_idx", table_name="email_attachments")
    op.drop_table("email_attachments")

    op.drop_constraint("email_domains_webhook_pair", "email_domains", type_="check")
    op.drop_constraint("email_domains_inbound_action", "email_domains", type_="check")
    for col in (
        "forward_rate_per_hour",
        "webhook_secret_encrypted",
        "webhook_url",
        "forward_to",
        "inbound_enabled",
    ):
        op.drop_column("email_domains", col)

    op.drop_index("emails_inbound_message_id_uq", table_name="emails")
    op.drop_index("emails_message_id_idx", table_name="emails")
    op.drop_index("emails_org_direction_created_idx", table_name="emails")

    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained')",
    )

    for col in (
        "dmarc_verdict",
        "spf_verdict",
        "dkim_verdict",
        "virus_verdict",
        "spam_verdict",
        "raw_s3_key",
        "references_ids",
        "in_reply_to",
        "message_id",
        "provider_received_at",
    ):
        op.drop_column("emails", col)

    op.drop_constraint("emails_outbound_has_domain", "emails", type_="check")
    op.alter_column("emails", "email_domain_id", nullable=False)
    op.drop_constraint("emails_direction_check", "emails", type_="check")
    op.drop_column("emails", "direction")
