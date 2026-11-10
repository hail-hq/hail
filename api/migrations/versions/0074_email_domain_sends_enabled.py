"""A per-domain switch to stop sending: ``email_domains.sends_enabled``.

An org that verified a custom domain got it forced on as a sender forever:
``resolve_sender`` picked it for every ``from``-less send, and an explicit
``from`` was the only way to use a different identity (the hail-mail
address). The only opt-out was deleting the domain, which needs an SES
round-trip and dies with 409 once the domain holds mail rows.

The switch belongs on ``email_domains`` (schema rules: extend the closest
table that already holds the same kind of record — this is a send-capable
variant next to ``inbound_enabled``, which already lives per row).

Column semantics — ``sends_enabled = false`` means this identity must not
send at all:

* ``verified_senders`` skips it, so ``from``-less sends fall back to the
  hail-mail address (or resolveSender's remaining cases) instead of
  preferring it. A pending custom row that is switched off also stops
  raising the "pending DKIM" 422.
* A send that names it explicitly is refused with 422. Before, disabling
  could not fail closed: the row is still ``verification_status='verified'``
  and the address stays deliverable-looking.
* Inbound mail and forward settings are untouched — the switch is one-way
  about sending only.

Default ``true``: every existing row keeps today's behavior, and hail-mail
rows may not be switched off (CHECK
``email_domains_sends_enabled_check``) — they are the fallback identity
system mail (forward confirmations) and a ``from``-less send resolves to,
so an org that disabled every custom domain must still be able to send.

Downgrade drops the CHECK and the column. Rows carry no other state, so
nothing to backfill: upgrade only adds the column with its default.

Revision ID: 0074
Revises: 0073
"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "email_domains",
        sa.Column(
            "sends_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "email_domains_sends_enabled_check",
        "email_domains",
        "kind <> 'hail_mail' OR sends_enabled",
    )


def downgrade() -> None:
    op.drop_constraint("email_domains_sends_enabled_check", "email_domains")
    op.drop_column("email_domains", "sends_enabled")
