"""Rename email_domains.dkim_records -> dns_records; add mail_from_status.

The records column now carries DKIM CNAMEs plus the custom MAIL FROM MX/SPF,
so the DKIM-specific name is misleading. mail_from_status tracks the SES
MAIL FROM verification independently of the DKIM-driven verification_status.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("email_domains", "dkim_records", new_column_name="dns_records")
    op.add_column(
        "email_domains",
        sa.Column("mail_from_status", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("email_domains", "mail_from_status")
    op.alter_column("email_domains", "dns_records", new_column_name="dkim_records")
