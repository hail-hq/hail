"""Allow only one live number verification with no name per subject type.

A verification form may hold no name of the person or company (then
``carrier_verifications.subject_name`` is NULL). The unique index
``carrier_verifications_number_live_uniq`` skips those rows, so the same
organization could file the same kind again and again, each time a new
filing with the carrier. This adds
``carrier_verifications_number_nameless_live_uniq`` on
(organization_id, provider, country_code, number_type, subject_type) for
``purpose = 'number'``, live states and ``subject_name IS NULL``. Rows with
a name keep the rule from the index above.

Tables checked (no new table, no new column): ``carrier_verifications``
already holds one row per verification; this is a rule on those rows.

Upgrade fails when an organization already holds two live nameless number
verifications of one subject type for the same key: cancel the extra ones
first. Downgrade drops the index.
"""

from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_number_nameless_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type, subject_type) "
        "WHERE purpose = 'number' AND state NOT IN ('cancelled','rejected') "
        "AND subject_name IS NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "carrier_verifications_number_nameless_live_uniq",
        table_name="carrier_verifications",
    )
