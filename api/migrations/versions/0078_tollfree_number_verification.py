"""Keep toll-free verification on the existing per-carrier verification row.

Tables checked: carrier_verifications already holds verification state and
provider refs; phone_numbers owns number identity. Auth tables (users,
organizations, sessions, api_keys) hold identity/access, not carrier review.
Reuse carrier_verifications and link the exact phone_numbers row. No new table.
The old SMS unique index excludes toll-free; a separate partial unique index
allows one live verification per toll-free number, regardless of country.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "carrier_verifications",
        sa.Column("number_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "carrier_verifications_number_id_fkey",
        "carrier_verifications",
        "phone_numbers",
        ["number_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "carrier_verifications_tollfree_number_check",
        "carrier_verifications",
        "purpose != 'sms' OR number_type != 'toll_free' OR number_id IS NOT NULL",
    )
    op.drop_index(
        "carrier_verifications_sms_live_uniq", table_name="carrier_verifications"
    )
    op.create_index(
        "carrier_verifications_sms_live_uniq",
        "carrier_verifications",
        ["organization_id", "provider", "country_code", "number_type"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'sms' AND number_type != 'toll_free' AND state NOT IN ('cancelled','rejected')"
        ),
    )
    op.create_index(
        "carrier_verifications_tollfree_live_uniq",
        "carrier_verifications",
        ["number_id"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'sms' AND number_type = 'toll_free' AND state NOT IN ('cancelled','rejected')"
        ),
    )


def downgrade():
    # Multiple live TFVs may now exist for a country. A downgrade must fail
    # rather than silently discard registrations to restore the old index.
    op.drop_index(
        "carrier_verifications_tollfree_live_uniq", table_name="carrier_verifications"
    )
    op.drop_index(
        "carrier_verifications_sms_live_uniq", table_name="carrier_verifications"
    )
    op.create_index(
        "carrier_verifications_sms_live_uniq",
        "carrier_verifications",
        ["organization_id", "provider", "country_code", "number_type"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'sms' AND state NOT IN ('cancelled','rejected')"
        ),
    )
    op.drop_constraint(
        "carrier_verifications_tollfree_number_check",
        "carrier_verifications",
        type_="check",
    )
    op.drop_constraint(
        "carrier_verifications_number_id_fkey",
        "carrier_verifications",
        type_="foreignkey",
    )
    op.drop_column("carrier_verifications", "number_id")
