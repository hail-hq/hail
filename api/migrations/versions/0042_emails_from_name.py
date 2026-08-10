"""Add emails.from_name — optional display name for the From: header.

Rendered as "Name <addr>" via email.utils.formataddr at send time.
NULL on inbound rows and on every send that predates the column.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE emails ADD COLUMN IF NOT EXISTS from_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE emails DROP COLUMN IF EXISTS from_name")
