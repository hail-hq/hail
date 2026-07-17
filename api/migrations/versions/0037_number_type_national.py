"""Add 'national' to phone_numbers.number_type.

Twilio offers a `national` number type for several countries (Japan, Brazil,
Romania, Czech Republic). Postgres can't widen a CHECK in place, so this drops
and re-adds phone_numbers_number_type_check. Existing rows only ever used
local/mobile/toll_free, a subset of the new set, so the re-add is safe with no
pre-flight check.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "phone_numbers_number_type_check", "phone_numbers", type_="check"
    )
    op.create_check_constraint(
        "phone_numbers_number_type_check",
        "phone_numbers",
        "number_type IN ('local','mobile','toll_free','national')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "phone_numbers_number_type_check", "phone_numbers", type_="check"
    )
    op.create_check_constraint(
        "phone_numbers_number_type_check",
        "phone_numbers",
        "number_type IN ('local','mobile','toll_free')",
    )
