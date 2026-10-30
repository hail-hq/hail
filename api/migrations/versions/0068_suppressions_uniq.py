"""Allow only one suppression row per (organization, recipient, channel).

``suppressions`` had no unique rule, so ``add_suppression`` added a new
row on every call: each unsubscribe click, each STOP reply, and
concurrent bounce events for one address. This deletes the duplicates,
keeping the oldest row of each group, then adds two partial unique
indexes. ``suppressions_org_recipient_channel_uniq`` covers org-scoped
rows. ``suppressions_platform_recipient_channel_uniq`` covers
platform-wide rows (``organization_id IS NULL``), because NULLs are
distinct in a plain unique index. ``add_suppression`` now inserts with
ON CONFLICT DO NOTHING.

Tables checked (no new table, no new column): ``suppressions`` already
holds one row per do-not-contact entry; this is a rule on those rows.

Downgrade drops both indexes. The deleted duplicates are not restored.
"""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PARTITION BY groups NULL organization_id rows together, so one
    # statement dedupes both org-scoped and platform-wide rows.
    op.execute(
        "DELETE FROM suppressions s USING ("
        "SELECT id, row_number() OVER ("
        "PARTITION BY organization_id, recipient, channel "
        "ORDER BY created_at, id) AS rn FROM suppressions"
        ") d WHERE s.id = d.id AND d.rn > 1"
    )
    op.execute(
        "CREATE UNIQUE INDEX suppressions_org_recipient_channel_uniq "
        "ON suppressions (organization_id, recipient, channel) "
        "WHERE organization_id IS NOT NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX suppressions_platform_recipient_channel_uniq "
        "ON suppressions (recipient, channel) "
        "WHERE organization_id IS NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "suppressions_platform_recipient_channel_uniq", table_name="suppressions"
    )
    op.drop_index("suppressions_org_recipient_channel_uniq", table_name="suppressions")
