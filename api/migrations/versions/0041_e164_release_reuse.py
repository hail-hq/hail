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

import sqlalchemy as sa
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
    # The full UNIQUE cannot be rebuilt once a released tombstone shares an
    # e164 with a live re-acquired row — the exact state this migration
    # exists to allow. Fail with an explanation instead of a bare
    # duplicate-key error halfway through a rollback.
    dup = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT e164 FROM phone_numbers"
                " GROUP BY e164 HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if dup is not None:
        raise RuntimeError(
            "cannot downgrade 0041: multiple phone_numbers rows share e164 "
            f"{dup[0]!r} (a released tombstone plus a re-acquired live row). "
            "Delete or re-number the tombstone rows first, then re-run the "
            "downgrade."
        )
    op.drop_index("phone_numbers_e164_live_uniq", table_name="phone_numbers")
    op.create_unique_constraint("phone_numbers_e164_key", "phone_numbers", ["e164"])
