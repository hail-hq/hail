"""Index sms by organization for trial texts.

Counts a workspace's trial texts (``trial_texts_used`` in
hailhq.core.sms_trial): outbound rows whose metadata has the ``sms_trial``
key.

Built CONCURRENTLY so texts keep writing to ``sms`` while it builds. That
cannot run in a transaction, hence the autocommit block. It is its own
revision so a failed build cannot leave 0081's table without its stamp.

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "sms_trial_org_idx",
            "sms",
            ["organization_id"],
            postgresql_where=text("metadata ? 'sms_trial'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "sms_trial_org_idx",
            table_name="sms",
            postgresql_concurrently=True,
        )
