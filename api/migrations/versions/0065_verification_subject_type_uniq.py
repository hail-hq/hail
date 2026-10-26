"""One live number verification per subject type.

An organization may hold a verification as a person and one as a business
for the same carrier, country and number type at the same time. The single
live unique index ``carrier_verifications_live_uniq`` on
(organization_id, provider, country_code, number_type, purpose) allowed
only one. It is replaced by two partial unique indexes:

- ``carrier_verifications_number_live_uniq``: one live row per
  (organization_id, provider, country_code, number_type, subject_type)
  for ``purpose = 'number'``.
- ``carrier_verifications_sms_live_uniq``: one live row per
  (organization_id, provider, country_code, number_type) for
  ``purpose = 'sms'``, as before (a US SMS registration is one per carrier).

"Live" means a state other than 'cancelled' or 'rejected', as before.

Tables checked (no new table): ``carrier_verifications`` already holds
the kind in ``subject_type``; only its uniqueness changes.

Downgrade restores the old index. It fails when an organization holds a
live person and a live business verification for the same key: cancel one
of them first.
"""

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

LIVE = "state NOT IN ('cancelled','rejected')"


def upgrade() -> None:
    op.drop_index("carrier_verifications_live_uniq", table_name="carrier_verifications")
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_number_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type, subject_type) "
        f"WHERE purpose = 'number' AND {LIVE}"
    )
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_sms_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type) "
        f"WHERE purpose = 'sms' AND {LIVE}"
    )


def downgrade() -> None:
    op.drop_index(
        "carrier_verifications_sms_live_uniq", table_name="carrier_verifications"
    )
    op.drop_index(
        "carrier_verifications_number_live_uniq", table_name="carrier_verifications"
    )
    op.execute(
        "CREATE UNIQUE INDEX carrier_verifications_live_uniq "
        "ON carrier_verifications "
        "(organization_id, provider, country_code, number_type, purpose) "
        f"WHERE {LIVE}"
    )
