"""drop per-domain webhook (consolidate onto WebhookSubscription)

Removes the per-domain webhook mechanism. ``email_domains.webhook_url`` and
``webhook_secret_encrypted`` are dropped; the inbound-action CHECK is
re-expressed to require ``forward_to`` (webhook is no longer a per-domain
action); ``webhook_deliveries`` now always carries a ``subscription_id`` so
its target CHECK tightens.

Guarded destructive migration: asserts zero rows currently use a per-domain
webhook before dropping (the consolidation design verified none exist). No
data backfill.

See docs/superpowers/specs/2026-06-24-webhook-consolidation-dashboard-design.md.

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Guard: refuse to drop if any per-domain webhook is actually configured.
    # Skipped in offline mode (`alembic upgrade head --sql`, used for the
    # deploy dry-print): there's no live connection to query there, and the
    # check is meaningless for a SQL preview — it only needs to run against
    # a real database.
    if not context.is_offline_mode():
        conn = op.get_bind()
        n = conn.execute(
            sa.text("SELECT count(*) FROM email_domains WHERE webhook_url IS NOT NULL")
        ).scalar_one()
        if n:
            raise RuntimeError(
                f"{n} email_domains row(s) still use webhook_url; migrate them to "
                "WebhookSubscription before dropping per-domain webhooks."
            )

    # Re-express the inbound-action invariant without webhook_url.
    op.drop_constraint("email_domains_inbound_action", "email_domains", type_="check")
    op.create_check_constraint(
        "email_domains_inbound_action",
        "email_domains",
        "NOT inbound_enabled OR forward_to IS NOT NULL",
    )
    op.drop_constraint("email_domains_webhook_pair", "email_domains", type_="check")
    op.drop_column("email_domains", "webhook_secret_encrypted")
    op.drop_column("email_domains", "webhook_url")

    # Every delivery now belongs to a subscription.
    op.drop_constraint(
        "webhook_deliveries_target_check", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "webhook_deliveries_target_check",
        "webhook_deliveries",
        "subscription_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "webhook_deliveries_target_check", "webhook_deliveries", type_="check"
    )
    op.create_check_constraint(
        "webhook_deliveries_target_check",
        "webhook_deliveries",
        "subscription_id IS NOT NULL OR email_domain_id IS NOT NULL",
    )
    op.add_column("email_domains", sa.Column("webhook_url", sa.Text()))
    op.add_column("email_domains", sa.Column("webhook_secret_encrypted", sa.Text()))
    op.create_check_constraint(
        "email_domains_webhook_pair",
        "email_domains",
        "(webhook_url IS NULL) = (webhook_secret_encrypted IS NULL)",
    )
    op.drop_constraint("email_domains_inbound_action", "email_domains", type_="check")
    op.create_check_constraint(
        "email_domains_inbound_action",
        "email_domains",
        "NOT inbound_enabled OR forward_to IS NOT NULL OR webhook_url IS NOT NULL",
    )
