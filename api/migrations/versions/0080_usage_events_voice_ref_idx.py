"""Index voice ``usage_events`` by organization and call id.

Each billed call and handover checks, under the organization's lock,
whether every ended call's usage row is priced yet (``_HELD_SQL`` in
hailhq.core.call_allowance). No index covered ``ref``, so each check read
all of the organization's usage rows. The index key is the call id part
of ``ref`` (before any ``:suffix``), as the check and the rater read it.

Built CONCURRENTLY so calls, texts and email keep writing to
``usage_events`` while it builds. That cannot run in a transaction, hence
the autocommit block.

Revision ID: 0080
Revises: 0079
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0080"
down_revision: str | None = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "usage_events_voice_ref_idx",
            "usage_events",
            ["organization_id", text("split_part(ref, ':', 1)")],
            postgresql_where=text("channel = 'voice'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "usage_events_voice_ref_idx",
            table_name="usage_events",
            postgresql_concurrently=True,
        )
