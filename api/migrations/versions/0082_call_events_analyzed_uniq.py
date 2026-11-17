"""One ``analyzed`` call event per call.

The ``call.analyzed`` webhook (``hailhq.core.call_analysis``) is sent once
per call. Its marker is a ``call_events`` row with ``kind = 'analyzed'``,
next to the call's other event rows, and the worker's claim is a row with
``kind = 'analyzing'``. No new table or column: ``call_events`` already
holds each step of a call. The sender locks the call row before it writes
the marker; this partial unique index makes the database refuse a second
marker too.

Built CONCURRENTLY so the voicebot keeps writing call events while it
builds. That cannot run in a transaction, hence the autocommit block.

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
            "call_events_analyzed_uniq",
            "call_events",
            ["call_id"],
            unique=True,
            postgresql_where=text("kind = 'analyzed'"),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "call_events_analyzed_uniq",
            table_name="call_events",
            postgresql_concurrently=True,
        )
