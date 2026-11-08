"""One ledger row per (organization, ref) in ``account_credits``.

Every ledger writer keys its row on ``ref`` (a usage event, a month's fee, a
Stripe payment, a verification hold) and skips a ref the organization already
has. Until now only the writers enforced that, with a read before the insert.
Two writers that overlap can both read "no row" and both insert, which charges
or credits the customer twice. A unique index makes the database refuse the
second row, so writers can use ``ON CONFLICT (organization_id, ref) DO
NOTHING``.

The index is not partial: Postgres treats NULL refs as distinct, so rows
without a ref are still allowed, and a plain ``ON CONFLICT (organization_id,
ref)`` finds the index without a ``WHERE`` clause.

``idx_account_credits_ref`` stays: this change adds a guard and removes
nothing.

Upgrade fails if a database already holds two rows with the same
(organization_id, ref). That stop is on purpose: the ledger is money, so the
migration does not pick a row to delete. Find the rows with
``SELECT organization_id, ref, count(*) FROM account_credits WHERE ref IS NOT
NULL GROUP BY 1, 2 HAVING count(*) > 1``, settle each by hand, then deploy
again.

Downgrade drops the index. No rows change.
"""

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

INDEX = "account_credits_org_ref_uq"


def upgrade() -> None:
    op.create_index(INDEX, "account_credits", ["organization_id", "ref"], unique=True)


def downgrade() -> None:
    op.drop_index(INDEX, table_name="account_credits")
