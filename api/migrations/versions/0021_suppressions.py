"""suppressions table — org-scoped or global do-not-contact list.

Backs the pre-send compliance gate (``hailhq.core.compliance_gate``):
suppressed ``(recipient, channel)`` or ``(recipient, 'all')`` pairs block a
send, org-scoped (``organization_id`` set) or platform-wide
(``organization_id IS NULL``). Populated by the unsubscribe link
(``GET /unsubscribe``), manual ops action, or a future bounce/complaint
handler. A voice row IS an internal DNC entry — no separate DNC table.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppressions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=True),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel IN ('voice','email','all')",
            name="suppressions_channel_check",
        ),
    )
    op.create_index(
        "suppressions_recipient_channel_idx",
        "suppressions",
        ["recipient", "channel"],
    )


def downgrade() -> None:
    op.drop_index("suppressions_recipient_channel_idx", table_name="suppressions")
    op.drop_table("suppressions")
