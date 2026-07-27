"""Global uniqueness for hail-mail addresses + forward-queue poll index
+ inbound provider_message_id dedupe index.

Inbound routing looks up email_domains by (local_prefix_user,
local_prefix_org) with NO org scoping — without this index two orgs can
hold the same hail-mail prefix pair and one intercepts the other's mail.
The index keys on the prefix pair (the actual routing key) rather than
the ``domain`` column so the guarantee survives a change of
HAIL_MAIL_BASE_DOMAIN.

hail_mail rows have existed since migration 0005 (``sender_domains``,
renamed in 0006) and may exist in production. This upgrade FAILS if
duplicate hail_mail prefix pairs already exist. Before upgrading, check:

    SELECT local_prefix_user, local_prefix_org, count(*)
    FROM email_domains WHERE kind = 'hail_mail'
    GROUP BY 1, 2 HAVING count(*) > 1;

If any rows come back, decide which organization keeps each address and
delete (or re-prefix) the others manually, then re-run the upgrade.

Also adds ``emails_inbound_provider_message_id_uq``: mail without a
Message-ID header used to bypass dedupe entirely, so SES at-least-once
redelivery duplicated rows. The SES receipt id (provider_message_id) is
what repeats on redelivery; a partial unique index on
(organization_id, provider_message_id) backs the fallback dedupe.
That index creation also FAILS if duplicate inbound rows already exist.
Before upgrading, check:

    SELECT organization_id, provider_message_id, count(*)
    FROM emails
    WHERE direction = 'inbound' AND provider_message_id IS NOT NULL
    GROUP BY 1, 2 HAVING count(*) > 1;

If any rows come back, delete the duplicate rows (keep one per pair),
then re-run the upgrade.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "email_domains_hail_mail_prefix_uq",
        "email_domains",
        ["local_prefix_user", "local_prefix_org"],
        unique=True,
        postgresql_where=sa.text("kind = 'hail_mail'"),
    )
    # The forward worker polls emails every second; keep that query
    # index-only. Direct-send rows pass through 'queued' only momentarily,
    # so the partial index stays tiny.
    op.create_index(
        "emails_forward_queue_idx",
        "emails",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued' AND direction = 'outbound'"),
    )
    # Dedupe fallback for inbound mail without a Message-ID header: the
    # SES receipt id repeats on redelivery, so it backs the same
    # idempotency guarantee, org-scoped.
    op.create_index(
        "emails_inbound_provider_message_id_uq",
        "emails",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "direction = 'inbound' AND provider_message_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("emails_inbound_provider_message_id_uq", table_name="emails")
    op.drop_index("emails_forward_queue_idx", table_name="emails")
    op.drop_index("email_domains_hail_mail_prefix_uq", table_name="email_domains")
