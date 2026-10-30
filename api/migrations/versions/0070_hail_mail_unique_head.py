"""One hail-mail head per deployment.

The head is the part of ``<head>+<tail>@<base>`` before the plus. Mail
clients and mail providers treat it as the mailbox, and the tail as an
alias. Inbound mail now routes on the head alone, so two rows must never
share one.

Rows that share a head are fixed first. The row with the most emails keeps
the head; on a tie, the oldest row keeps it. Each other row gets the new head
``<first 11 chars>-<8 hex chars>`` and a matching ``domain``. Then a partial
unique index on the head replaces ``email_domains_hail_mail_prefix_uq``: a
unique head makes every (head, tail) pair unique, so nothing needs the old
index.

Tables checked (no new table, no new column): ``email_domains`` already holds
the head; this is a rule on those rows.

Downgrade swaps the indexes back. It cannot restore a rewritten address: the
old head is not stored, and mail to it stops arriving at upgrade.
"""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A row inserted between the rewrite and the index would fail the deploy.
    op.execute("LOCK TABLE email_domains IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""
        WITH ranked AS (
            SELECT d.id, row_number() OVER (
                PARTITION BY d.local_prefix_user
                ORDER BY (
                    SELECT count(*) FROM emails e WHERE e.email_domain_id = d.id
                ) DESC, d.created_at, d.id
            ) AS rn
            FROM email_domains d
            WHERE d.kind = 'hail_mail'
        ), renamed AS (
            SELECT d.id,
                   rtrim(left(d.local_prefix_user, 11), '-') || '-'
                       || substr(md5(d.id::text), 1, 8) AS head
            FROM email_domains d
            JOIN ranked r ON r.id = d.id
            WHERE r.rn > 1
        )
        UPDATE email_domains d
           SET local_prefix_user = r.head,
               domain = r.head || '+' || d.local_prefix_org
                   || substr(d.domain, position('@' in d.domain)),
               updated_at = now()
          FROM renamed r
         WHERE d.id = r.id
        """)
    op.execute("DROP INDEX email_domains_hail_mail_prefix_uq")
    op.execute(
        "CREATE UNIQUE INDEX email_domains_hail_mail_head_uq "
        "ON email_domains (local_prefix_user) WHERE kind = 'hail_mail'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX email_domains_hail_mail_head_uq")
    op.execute(
        "CREATE UNIQUE INDEX email_domains_hail_mail_prefix_uq "
        "ON email_domains (local_prefix_user, local_prefix_org) "
        "WHERE kind = 'hail_mail'"
    )
