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


def _rename(
    old_index: str,
    new_index: str,
    new_marker: str,
    kinds: dict[str, str],
    old_event: str,
    new_event: str,
) -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            new_index,
            "call_events",
            ["call_id"],
            unique=True,
            postgresql_where=text(f"kind = '{new_marker}'"),
            postgresql_concurrently=True,
        )
    for old_kind, kind in kinds.items():
        op.execute(
            text("UPDATE call_events SET kind = :new WHERE kind = :old").bindparams(
                new=kind, old=old_kind
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
        op.drop_index(old_index, table_name="call_events", postgresql_concurrently=True)


def upgrade() -> None:
    _rename(
        "call_events_analyzed_uniq",
        "call_events_transcribed_uniq",
        "transcribed",
        {"analyzed": "transcribed", "analyzing": "transcribing"},
        "call.analyzed",
        "call.transcribed",
    )


def downgrade() -> None:
    _rename(
        "call_events_transcribed_uniq",
        "call_events_analyzed_uniq",
        "analyzed",
        {"transcribed": "analyzed", "transcribing": "analyzing"},
        "call.transcribed",
        "call.analyzed",
    )
