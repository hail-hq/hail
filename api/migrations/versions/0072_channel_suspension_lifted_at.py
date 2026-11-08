"""Keep a lifted channel suspension, with the time it was lifted.

Lifting a suspension used to delete its row, so the abuse monitor could not
tell when an org was cleared and counted the same old bounces again on its
next tick. Now an operator sets ``lifted_at`` instead. A suspension is
active while ``lifted_at`` is NULL, and the email check counts only mail
sent after the org's last lift.

* ``channel_suspensions.lifted_at``: new nullable column.
* The unique (organization_id, channel) constraint becomes a partial unique
  index on active rows, so an org can be suspended again after a lift.
* ``emails_outbound_sent_at_idx``: the hourly email abuse check reads
  outbound mail by ``sent_at``; without it, each check scans ``emails``.
  Built CONCURRENTLY so writes to ``emails`` do not block; that cannot run
  in a transaction, hence the autocommit block.

Tables checked: this extends ``channel_suspensions`` itself, the table that
holds these rows. No new table.

Downgrade deletes lifted rows (they no longer block anything), restores
the old constraint, and drops the index.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The concurrent build runs first, outside the transaction. If it fails
    # it can leave an INVALID index; the DROP IF EXISTS lets a re-run of
    # this migration start clean. The steps after it are one transaction.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS emails_outbound_sent_at_idx")
        op.create_index(
            "emails_outbound_sent_at_idx",
            "emails",
            ["sent_at"],
            postgresql_where=sa.text("direction = 'outbound'"),
            postgresql_concurrently=True,
        )
    op.add_column(
        "channel_suspensions",
        sa.Column("lifted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "channel_suspensions_lifted_after_suspended_check",
        "channel_suspensions",
        "lifted_at IS NULL OR lifted_at >= suspended_at",
    )
    op.drop_constraint(
        "channel_suspensions_org_channel_uniq", "channel_suspensions", type_="unique"
    )
    op.create_index(
        "channel_suspensions_active_uq",
        "channel_suspensions",
        ["organization_id", "channel"],
        unique=True,
        postgresql_where=sa.text("lifted_at IS NULL"),
    )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "emails_outbound_sent_at_idx",
            table_name="emails",
            postgresql_concurrently=True,
        )
    op.drop_index("channel_suspensions_active_uq", table_name="channel_suspensions")
    op.execute("DELETE FROM channel_suspensions WHERE lifted_at IS NOT NULL")
    op.create_unique_constraint(
        "channel_suspensions_org_channel_uniq",
        "channel_suspensions",
        ["organization_id", "channel"],
    )
    op.drop_constraint(
        "channel_suspensions_lifted_after_suspended_check",
        "channel_suspensions",
        type_="check",
    )
    op.drop_column("channel_suspensions", "lifted_at")
