"""Mark the pool numbers that send US trial texts.

A trial texting number is a Hail-owned pool number (``is_pool``, no
organization), the same kind of record as the shared call pool, so it stays
a ``phone_numbers`` row. The new ``pool_purpose`` column tells the two apart:

* NULL: the shared call pool. Every pool row today, and the row the
  self-host INSERT in docs/public/self-host/operations.md makes.
* ``'sms_trial'``: sends only trial texts (hailhq.core.sms_trial). Never
  claimed for calls or browser sessions.

Tables checked: ``sms_trial_phones`` holds each workspace's verified phone,
not Hail's senders. ``carrier_verifications`` holds an organization's
filings, and a pool number has no organization. No other table holds phone
numbers.

The CHECK keeps a marked number a toll-free pool number with no call
reservation: trial texts need toll-free verification, and a trial number
is never handed out for a call.

Revision ID: 0088
Revises: 0087
"""

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("phone_numbers", sa.Column("pool_purpose", sa.Text(), nullable=True))
    op.create_check_constraint(
        "phone_numbers_pool_purpose_check",
        "phone_numbers",
        "pool_purpose IS NULL OR (pool_purpose = 'sms_trial' AND is_pool"
        " AND reserved_call_id IS NULL AND number_type = 'toll_free')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "phone_numbers_pool_purpose_check", "phone_numbers", type_="check"
    )
    op.drop_column("phone_numbers", "pool_purpose")
