"""Let a saved agent email the person on the call (``agents.email_caller_enabled``).

An agent setting, like ``voice_enabled`` or ``email_enabled``: a boolean
on ``agents``, off by default so existing agents do not change. Tables
checked: ``agents`` holds every per-agent setting, so the switch goes
there. The emails it sends are ordinary ``emails`` rows (marked in
``metadata``), and what happened on the call is a ``call_events`` row;
neither needs a new column. A partial index on ``emails`` keeps the tool's
caps (per call, per caller number per day, per org per day) cheap. It is
built CONCURRENTLY so sends and inbound mail keep writing to ``emails``
while it builds; that cannot run in a transaction, hence the autocommit
block. A partial unique index on ``call_events`` keeps one ``email_caller``
event per send (rule 5: one table, several kinds of row).

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0082"
down_revision: str | None = "0081"
branch_labels = None
depends_on = None

_INDEXES = (
    (
        "emails_caller_email_idx",
        "emails",
        ["organization_id", "requested_at"],
        "(metadata ->> 'email_caller') = 'true'",
        False,
    ),
    (
        "call_events_email_caller_uq",
        "call_events",
        ["call_id", sa.text("(payload ->> 'tool_invocation_id')")],
        "kind = 'email_caller'",
        True,
    ),
)


def upgrade() -> None:
    # The column first, inside env.py's transaction and its lock and
    # statement timeouts. IF NOT EXISTS: a rerun after a failed index build
    # below finds it already there.
    op.execute(
        "ALTER TABLE agents ADD COLUMN IF NOT EXISTS email_caller_enabled "
        "BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Then the indexes, CONCURRENTLY (no transaction). A failed concurrent
    # build leaves an INVALID index, so each build drops any old one first:
    # a rerun rebuilds it instead of keeping an index the planner never uses.
    with op.get_context().autocommit_block():
        for name, table, columns, where, unique in _INDEXES:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
            op.create_index(
                name,
                table,
                columns,
                unique=unique,
                postgresql_where=sa.text(where),
                postgresql_concurrently=True,
            )
    # The block committed env.py's transaction and its SET LOCAL guards. Set
    # them again for any later migration in the same run.
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL lock_timeout = '5s'")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name, table, *_ in _INDEXES:
            op.drop_index(
                name, table_name=table, postgresql_concurrently=True, if_exists=True
            )
    op.drop_column("agents", "email_caller_enabled")
