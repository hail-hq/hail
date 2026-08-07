"""Make phone_numbers.e164 uniqueness partial: released rows are tombstones.

Release (0040's dunning flow, DELETE /numbers/{id}) keeps the row for
billing history. With the full UNIQUE from 0001, re-acquiring a number
Twilio later recycles would purchase it at the carrier and then fail the
INSERT — a 500, an orphaned paid number, and a stuck idempotency key.
Only non-released rows need to be unique.

Revision ID: 0041
Revises: 0040
"""

from __future__ import annotations

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("phone_numbers_e164_key", "phone_numbers", type_="unique")
    op.create_index(
        "phone_numbers_e164_live_uniq",
        "phone_numbers",
        ["e164"],
        unique=True,
        postgresql_where="provisioning_state <> 'released'",
    )


def downgrade() -> None:
    op.drop_index("phone_numbers_e164_live_uniq", table_name="phone_numbers")
    op.create_unique_constraint("phone_numbers_e164_key", "phone_numbers", ["e164"])
