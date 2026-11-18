"""Rename the ``call.analyzed`` webhook to ``call.transcribed``.

The event's marker row in ``call_events`` moves from ``kind = 'analyzed'``
to ``kind = 'transcribed'``, and the worker's claim from ``'analyzing'`` to
``'transcribing'``. ``GET /v1/events`` shows the marker kind, so it matches
the event name. A partial index's predicate cannot be changed, so the
unique index of 0082 is replaced: build the new one, move the rows, drop
the old one. Subscriptions that list ``call.analyzed`` get
``call.transcribed`` in its place. Their ``updated_at`` stays as it is, so
the calls they wait for are still sent.

The indexes are built and dropped CONCURRENTLY so the voicebot keeps
writing call events. That cannot run in a transaction, hence the
autocommit blocks.

Revision ID: 0083
Revises: 0082
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0083"
down_revision: str | None = "0082"
branch_labels = None
depends_on = None


def _rename(old: tuple[str, str, str], new: tuple[str, str, str]) -> None:
    """Move (marker kind, claim kind, event name) from ``old`` to ``new``."""
    (old_marker, old_claim, old_event), (new_marker, new_claim, new_event) = old, new
    with op.get_context().autocommit_block():
        # A failed earlier run can leave the new index behind, maybe INVALID.
        op.drop_index(
            f"call_events_{new_marker}_uniq",
            table_name="call_events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.create_index(
            f"call_events_{new_marker}_uniq",
            "call_events",
            ["call_id"],
            unique=True,
            postgresql_where=text(f"kind = '{new_marker}'"),
            postgresql_concurrently=True,
        )
    # The autocommit block ended env.py's transaction and its SET LOCAL
    # guards; set them again for the updates.
    op.execute("SET LOCAL statement_timeout = '120s'")
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        text(
            "UPDATE call_events"
            " SET kind = CASE kind WHEN :old_marker THEN :new_marker"
            " ELSE :new_claim END"
            " WHERE kind IN (:old_marker, :old_claim)"
        ).bindparams(
            old_marker=old_marker,
            new_marker=new_marker,
            old_claim=old_claim,
            new_claim=new_claim,
        )
    )
    op.execute(
        text(
            "UPDATE webhook_subscriptions"
            " SET event_types = array_replace(event_types, :old, :new)"
            " WHERE :old = ANY(event_types)"
        ).bindparams(old=old_event, new=new_event)
    )
    # Commit the moves before the old index goes.
    with op.get_context().autocommit_block():
        op.drop_index(
            f"call_events_{old_marker}_uniq",
            table_name="call_events",
            postgresql_concurrently=True,
            if_exists=True,
        )


OLD = ("analyzed", "analyzing", "call.analyzed")
NEW = ("transcribed", "transcribing", "call.transcribed")


def upgrade() -> None:
    _rename(OLD, NEW)


def downgrade() -> None:
    _rename(NEW, OLD)
