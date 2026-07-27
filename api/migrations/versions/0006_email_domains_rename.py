"""rename sender_domains to email_domains

Phase 1 of the inbound-email milestone: ``sender_domains`` now also
identifies inbound receiving identities, so the name no longer fits.
Pure rename — no data change, no constraint shape change. Renames the
table, its PK and unique-constraint indexes, the auxiliary ``idx_*``
indexes, the ``updated_at`` trigger, the three CHECK constraints, the
FK column on ``emails``, and the matching FK constraint.

See ``docs/superpowers/specs/2026-06-06-inbound-email-design.md``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("sender_domains", "email_domains")
    op.execute("ALTER INDEX sender_domains_pkey RENAME TO email_domains_pkey")
    op.execute(
        "ALTER INDEX sender_domains_org_domain_unique "
        "RENAME TO email_domains_org_domain_unique"
    )
    op.alter_column("emails", "sender_domain_id", new_column_name="email_domain_id")
    op.execute(
        "ALTER TABLE emails RENAME CONSTRAINT emails_sender_domain_id_fkey "
        "TO emails_email_domain_id_fkey"
    )
    op.execute("ALTER INDEX idx_sender_domains_org RENAME TO idx_email_domains_org")
    op.execute(
        "ALTER INDEX idx_sender_domains_org_verified "
        "RENAME TO idx_email_domains_org_verified"
    )
    op.execute("ALTER INDEX idx_emails_sender_domain RENAME TO idx_emails_email_domain")
    op.execute(
        "ALTER TRIGGER sender_domains_updated_at ON email_domains "
        "RENAME TO email_domains_updated_at"
    )
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT sender_domains_kind_check "
        "TO email_domains_kind_check"
    )
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT "
        "sender_domains_prefix_kind_consistency TO "
        "email_domains_prefix_kind_consistency"
    )
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT "
        "sender_domains_verification_status_check TO "
        "email_domains_verification_status_check"
    )


def downgrade() -> None:
    # Constraints + trigger must be renamed back BEFORE the table is
    # renamed, because the statements reference the table by its
    # current (post-upgrade) name ``email_domains``.
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT "
        "email_domains_verification_status_check TO "
        "sender_domains_verification_status_check"
    )
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT "
        "email_domains_prefix_kind_consistency TO "
        "sender_domains_prefix_kind_consistency"
    )
    op.execute(
        "ALTER TABLE email_domains RENAME CONSTRAINT email_domains_kind_check "
        "TO sender_domains_kind_check"
    )
    op.execute(
        "ALTER TRIGGER email_domains_updated_at ON email_domains "
        "RENAME TO sender_domains_updated_at"
    )
    op.execute(
        "ALTER INDEX IF EXISTS idx_emails_email_domain "
        "RENAME TO idx_emails_sender_domain"
    )
    op.execute(
        "ALTER INDEX IF EXISTS idx_email_domains_org_verified "
        "RENAME TO idx_sender_domains_org_verified"
    )
    op.execute(
        "ALTER INDEX IF EXISTS idx_email_domains_org RENAME TO idx_sender_domains_org"
    )
    op.execute(
        "ALTER TABLE emails RENAME CONSTRAINT emails_email_domain_id_fkey "
        "TO emails_sender_domain_id_fkey"
    )
    op.alter_column("emails", "email_domain_id", new_column_name="sender_domain_id")
    op.execute(
        "ALTER INDEX IF EXISTS email_domains_org_domain_unique "
        "RENAME TO sender_domains_org_domain_unique"
    )
    op.execute("ALTER INDEX IF EXISTS email_domains_pkey RENAME TO sender_domains_pkey")
    op.rename_table("email_domains", "sender_domains")
