"""Index ``emails.email_domain_id``.

The console domains page asks, per custom domain, whether any email links
to it (an EXISTS query). It hides Delete when the answer is yes, because
DELETE /email-domains answers 409 while emails still link to the domain.
No index covered that column alone: the existing ones are partial
(inbound custom rows only). Without one, a domain with no mail made
Postgres read every row of ``emails``.

Built CONCURRENTLY so sends and inbound mail keep writing to ``emails``
while it builds. That cannot run in a transaction, hence the autocommit
block.

Revision ID: 0077
Revises: 0076
"""

from __future__ import annotations

from alembic import op

revision: str = "0077"
down_revision: str | None = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "emails_email_domain_id_idx",
            "emails",
            ["email_domain_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "emails_email_domain_id_idx",
            table_name="emails",
            postgresql_concurrently=True,
        )
