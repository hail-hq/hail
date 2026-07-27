"""Split inbound dedup indexes by email_domain_kind.

Previously the two inbound dedup indexes were org-scoped:

    emails_inbound_message_id_uq        (organization_id, message_id)
    emails_inbound_provider_message_id_uq (organization_id, provider_message_id)

A single message addressed to two *custom* domains in the same org now
produces one Email row per receiving domain.  The org-scoped indexes blocked
that by treating (org, message_id) as a global uniqueness key even across
different domains.

This migration adds a denormalised ``email_domain_kind`` column, backfills it
from ``email_domains``, drops the old indexes, and creates four replacement
partial indexes that scope dedup by kind:

  hail_mail rows  → org-scoped  (one row per org, unchanged behaviour)
  custom rows     → domain-scoped (one row per receiving domain)

NULL ``email_domain_kind`` (outbound emails, or legacy inbound rows whose
``email_domain_id`` has since been deleted) are excluded from all four
partial indexes, preserving safe no-op semantics for those rows.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add the denormalised kind column (nullable — outbound rows don't need
    #    it, and we can't guarantee every inbound row still has its domain).
    op.add_column("emails", sa.Column("email_domain_kind", sa.Text(), nullable=True))

    # 2. Backfill from email_domains for all inbound rows that still have a
    #    live email_domain_id.  Rows whose domain was deleted remain NULL and
    #    fall outside the new partial indexes (safe: they can't produce a
    #    dedup collision anyway).
    op.execute("""
        UPDATE emails
        SET    email_domain_kind = ed.kind
        FROM   email_domains ed
        WHERE  emails.email_domain_id = ed.id
          AND  emails.direction = 'inbound'
        """)

    # 3. Drop the old org-scoped inbound dedup indexes.
    op.drop_index("emails_inbound_message_id_uq", table_name="emails")
    op.drop_index("emails_inbound_provider_message_id_uq", table_name="emails")

    # 4. Create four replacement partial indexes.
    op.create_index(
        "emails_hailmail_inbound_message_id_uq",
        "emails",
        ["organization_id", "message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND message_id IS NOT NULL"
            " AND email_domain_kind = 'hail_mail'"
        ),
    )
    op.create_index(
        "emails_custom_inbound_message_id_uq",
        "emails",
        ["email_domain_id", "message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND message_id IS NOT NULL"
            " AND email_domain_kind = 'custom'"
        ),
    )
    op.create_index(
        "emails_hailmail_inbound_pmid_uq",
        "emails",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND provider_message_id IS NOT NULL"
            " AND email_domain_kind = 'hail_mail'"
        ),
    )
    op.create_index(
        "emails_custom_inbound_pmid_uq",
        "emails",
        ["email_domain_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND provider_message_id IS NOT NULL"
            " AND email_domain_kind = 'custom'"
        ),
    )


def downgrade() -> None:
    # WARNING: lossy once the feature has run.  If the live DB accumulated two
    # inbound rows sharing the same message_id or provider_message_id — e.g. one
    # custom row and one hail_mail row (or two custom rows for the same org) —
    # recreating the org-scoped unique indexes below will fail with a unique
    # violation.  Manual deduplication is required before downgrading in that
    # case.
    op.drop_index("emails_custom_inbound_pmid_uq", table_name="emails")
    op.drop_index("emails_hailmail_inbound_pmid_uq", table_name="emails")
    op.drop_index("emails_custom_inbound_message_id_uq", table_name="emails")
    op.drop_index("emails_hailmail_inbound_message_id_uq", table_name="emails")

    op.create_index(
        "emails_inbound_provider_message_id_uq",
        "emails",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND provider_message_id IS NOT NULL"
        ),
    )
    op.create_index(
        "emails_inbound_message_id_uq",
        "emails",
        ["organization_id", "message_id"],
        unique=True,
        postgresql_where=sa.text("direction = 'inbound' AND message_id IS NOT NULL"),
    )

    op.drop_column("emails", "email_domain_kind")
