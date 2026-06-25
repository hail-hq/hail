"""tighten webhook delivery ownership

After consolidating onto ``WebhookSubscription`` (0013), every
``webhook_deliveries`` row is subscription-owned and ``email_domain_id`` is
purely informational (it stamps the ``X-Hail-Email-Domain`` header, not a
routing target). This migration moves those facts into the schema:

* ``subscription_id`` becomes ``NOT NULL`` and the degenerate
  ``webhook_deliveries_target_check`` (left by 0013 as
  ``subscription_id IS NOT NULL``) is dropped — the column carries the
  invariant now.
* the ``email_domain_id`` FK drops ``ON DELETE CASCADE`` (introduced in 0008
  when the domain owned the delivery) for ``ON DELETE SET NULL``, so deleting
  an email domain no longer takes unrelated delivery audit/retry rows with it.

The ``SET NOT NULL`` is safe on live data: 0013's CHECK already guaranteed no
NULL ``subscription_id`` rows exist.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # subscription_id is the sole owner — promote the CHECK to a column NOT NULL.
    # Order matters: on PG >= 12, SET NOT NULL skips the full-table scan when a
    # valid CHECK already proves non-null, so alter *before* dropping the CHECK.
    op.alter_column("webhook_deliveries", "subscription_id", nullable=False)
    op.drop_constraint(
        "webhook_deliveries_target_check", "webhook_deliveries", type_="check"
    )

    # email_domain_id is informational; don't cascade-delete deliveries with it.
    op.drop_constraint(
        "webhook_deliveries_email_domain_id_fkey",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "webhook_deliveries_email_domain_id_fkey",
        "webhook_deliveries",
        "email_domains",
        ["email_domain_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "webhook_deliveries_email_domain_id_fkey",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "webhook_deliveries_email_domain_id_fkey",
        "webhook_deliveries",
        "email_domains",
        ["email_domain_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.alter_column("webhook_deliveries", "subscription_id", nullable=True)
    op.create_check_constraint(
        "webhook_deliveries_target_check",
        "webhook_deliveries",
        "subscription_id IS NOT NULL",
    )
