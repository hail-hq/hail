"""Name the person or company a verification is for; allow several per kind.

Adds ``carrier_verifications.subject_name`` (text, nullable): the name of
the person or company, taken from the verification form. It is the only
personal value Hail keeps for a verification. Rows made before this column
have no name until the operator backfill fills it from the carrier
(``python -m hailhq.core.verification_names``).

An organization may now hold several live number verifications of one
subject type for the same carrier, country and number type (several
people, several companies). The unique index
``carrier_verifications_number_live_uniq`` on
(organization_id, provider, country_code, number_type, subject_type) is
replaced by one that blocks only an exact duplicate: the same subject type
and the same name, ignoring case
(..., subject_type, lower(subject_name)) for ``purpose = 'number'``, live
states, and a known name. ``carrier_verifications_sms_live_uniq`` stays.

Tables checked (no new table): ``carrier_verifications`` already holds one
row per verification with its ``subject_type``; the name describes that
same row. ``phone_numbers`` keeps the name the purchase used in
``provisioning_metadata.offer``, as it does the subject type.

Downgrade drops the column and restores the old index. It fails when an
organization holds two live number verifications of the same subject type
for the same key: cancel the extra ones first.
"""

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None

LIVE = "state NOT IN ('cancelled','rejected')"


def upgrade() -> None:
    op.add_column(
        "carrier_verifications", sa.Column("subject_name", sa.Text(), nullable=True)
    )
    op.drop_index(
        "carrier_verifications_number_live_uniq", table_name="carrier_verifications"
    )
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_number_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type, subject_type, "
        "lower(subject_name)) "
        f"WHERE purpose = 'number' AND {LIVE} AND subject_name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index(
        "carrier_verifications_number_live_uniq", table_name="carrier_verifications"
    )
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_number_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type, subject_type) "
        f"WHERE purpose = 'number' AND {LIVE}"
    )
    op.drop_column("carrier_verifications", "subject_name")
