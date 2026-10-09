"""Forward-target verification and complaint stop.

``email_domains.forward_to`` accepted any address. A tenant could point a
Hail inbox at a stranger, subscribe the inbox to newsletters, and Hail
relayed that mail under its own DKIM signature. One spam click at the
stranger's provider lands on the shared sender domain's reputation.

One row per (organization, address) with a status:

* ``pending``  — a confirm link was sent, not yet clicked; forwards skip it.
* ``verified`` — a verified member's login email, or the link was clicked.
* ``stopped``  — a spam complaint came back on a forward; forwards skip it
  until the address re-confirms by link.

Backfill marks every address already in ``forward_to`` as verified so
nothing changes for existing tenants.

Revision ID: 0055
Revises: 0054
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels = None
depends_on = None

_T = "email_forward_targets"


def upgrade() -> None:
    op.create_table(
        _T,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("token_hash", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_reason", sa.Text(), nullable=True),
        sa.Column("stopped_email_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending','verified','stopped')",
            name="email_forward_targets_status_check",
        ),
        sa.UniqueConstraint(
            "organization_id", "address", name="email_forward_targets_org_address_uq"
        ),
    )
    op.create_index(
        "email_forward_targets_token_hash_idx",
        _T,
        ["token_hash"],
        unique=True,
        postgresql_where=sa.text("token_hash IS NOT NULL"),
    )
    # Grandfather every address already configured: existing tenants keep
    # forwarding exactly as before this migration.
    op.execute(f"""
        INSERT INTO {_T} (organization_id, address, status, verified_at)
        SELECT DISTINCT d.organization_id, lower(trim(a.address)), 'verified', now()
        FROM email_domains d
        CROSS JOIN LATERAL unnest(d.forward_to) AS a(address)
        WHERE d.forward_to IS NOT NULL AND trim(a.address) <> ''
        ON CONFLICT (organization_id, address) DO NOTHING
        """)


def downgrade() -> None:
    op.drop_index("email_forward_targets_token_hash_idx", _T)
    op.drop_table(_T)
