"""account_credits.amount_cents: INTEGER -> NUMERIC(14,1).

The website rater now bills email at 0.2 cents per message and rounds all
usage up to 0.1 cent (was: whole cents). One decimal place of precision is
sufficient by design; NUMERIC(14,1) keeps existing integer values exact.

Downgrade is lossy (fractional rows would be rounded), so it refuses to run
while any fractional row exists.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "account_credits",
        "amount_cents",
        type_=sa.Numeric(14, 1),
        existing_type=sa.Integer(),
        existing_nullable=False,
        postgresql_using="amount_cents::numeric(14,1)",
    )


def downgrade() -> None:
    fractional = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM account_credits "
                "WHERE amount_cents <> round(amount_cents)"
            )
        )
        .scalar_one()
    )
    if fractional:
        raise RuntimeError(
            f"cannot downgrade: {fractional} account_credits rows have "
            "fractional cents; rounding them would corrupt balances"
        )
    op.alter_column(
        "account_credits",
        "amount_cents",
        type_=sa.Integer(),
        existing_type=sa.Numeric(14, 1),
        existing_nullable=False,
        postgresql_using="round(amount_cents)::integer",
    )
