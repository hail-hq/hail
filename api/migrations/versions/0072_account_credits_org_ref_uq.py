"""One ledger row per (organization, ref) in ``account_credits``.

Every ledger writer keys its row on ``ref`` (a usage event, a month's fee, a
Stripe payment, a verification hold) and skips a ref the organization already
has. Until now only the writers enforced that, with a read before the insert.
Two writers that overlap can both read "no row" and both insert, which charges
or credits the customer twice. A unique index makes the database refuse the
second row, so writers can use ``ON CONFLICT (organization_id, ref) DO
NOTHING``.

The index is not partial, although the table also holds rows with no ref
(manual adjustments). Postgres treats NULL refs as distinct, so those rows are
still allowed, and a plain ``ON CONFLICT (organization_id, ref)`` finds the
index without a ``WHERE`` clause. A partial index would make every writer
repeat its ``WHERE`` in the conflict target.

Built in the migration's transaction, not CONCURRENTLY: a failed concurrent
build of a unique index leaves an invalid index behind that must be dropped by
hand. On a ledger this size the build holds its write lock briefly. If an open
ledger transaction makes it wait past the lock timeout, the deploy stops with
nothing changed and can run again.

``idx_account_credits_ref`` (``ref`` alone, partial) is dropped. Every ledger
lookup filters on the organization and the ref, which the new index serves, so
the old one only cost writes.

Upgrade fails if a database already holds two rows with the same
(organization_id, ref). That stop is on purpose: the ledger is money, so the
migration does not pick a row to delete. Find the rows with
``SELECT organization_id, ref, count(*) FROM account_credits WHERE ref IS NOT
NULL GROUP BY 1, 2 HAVING count(*) > 1``, settle each by hand, then deploy
again.

Downgrade recreates ``idx_account_credits_ref`` as it was, then drops the
unique index. No rows change.
"""

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None

INDEX = "account_credits_org_ref_uq"
OLD_INDEX = "idx_account_credits_ref"


def upgrade() -> None:
    op.create_index(INDEX, "account_credits", ["organization_id", "ref"], unique=True)
    op.drop_index(OLD_INDEX, table_name="account_credits")


def downgrade() -> None:
    op.execute(
        f"CREATE INDEX {OLD_INDEX} ON account_credits (ref) WHERE ref IS NOT NULL"
    )
    op.drop_index(INDEX, table_name="account_credits")
