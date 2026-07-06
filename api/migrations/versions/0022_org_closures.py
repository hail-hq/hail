"""org_closures table — local record of hail-website account closures.

hail's own DB does not own account/org lifecycle (that lives in
hail-website's separate Postgres, cross-referenced only by a bare
organization_id with no FK — same posture as ``members``/``api_keys``).
This table is the local record hail-website writes to via
``POST /internal/org-closures`` when an account is closed/deleted, so
``hailhq.core.retention.purge_expired_data`` can find orgs past the
account-duration + 12-months retention window.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_closures",
        sa.Column("organization_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("org_closures")
