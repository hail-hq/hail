"""channel_suspensions table — per-org, per-channel sending pause.

Backs the abuse-monitoring guardrail described in the SMS design spec:
one platform-level 10DLC campaign means one org's abusive traffic can get
everyone throttled, so a targeted pause on just that org+channel is the
mitigation. Distinct from org_closures (whole-account) and suppressions
(per-recipient).

Revision ID: 0028
Revises: 0027
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_suspensions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "suspended_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('sms','voice','email')",
            name="channel_suspensions_channel_check",
        ),
        sa.UniqueConstraint(
            "organization_id", "channel", name="channel_suspensions_org_channel_uniq"
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_suspensions")
