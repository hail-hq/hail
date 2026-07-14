"""channel_suspensions table — per-org, per-channel sending pause.

Backs the abuse-monitoring guardrail described in the SMS design spec:
one platform-level 10DLC campaign means one org's abusive traffic can get
everyone throttled, so a targeted pause on just that org+channel is the
mitigation. Distinct from org_closures (whole-account) and suppressions
(per-recipient).

Revision ID: 0029
Revises: 0028

NOTE: originally authored as 0028; renumbered to 0029 to sit after the
parallel-branch 0028 (org_provider_config) that took the 0028 slot on main.
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0029"
down_revision: Union[str, None] = "0028"
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

    # Supporting indexes for the query shapes this feature introduces, so
    # they range-scan instead of seq-scanning tables that grow unbounded:
    #  - GET /sms/suppressions keyset-paginates per org+channel on created_at
    #    (mirrors idx_sms_org_created on the sms table).
    #  - the abuse monitor's hourly aggregates window on (channel, source,
    #    created_at) over suppressions and (channel, occurred_at) over the
    #    append-only usage_events table.
    op.create_index(
        "idx_suppressions_org_channel_created",
        "suppressions",
        ["organization_id", "channel", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_suppressions_channel_source_created",
        "suppressions",
        ["channel", "source", "created_at"],
    )
    op.create_index(
        "idx_usage_events_channel_time",
        "usage_events",
        ["channel", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_usage_events_channel_time", table_name="usage_events")
    op.drop_index("idx_suppressions_channel_source_created", table_name="suppressions")
    op.drop_index("idx_suppressions_org_channel_created", table_name="suppressions")
    op.drop_table("channel_suspensions")
