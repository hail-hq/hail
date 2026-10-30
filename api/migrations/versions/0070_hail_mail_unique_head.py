"""One hail-mail address per org, one head per deployment, retired addresses.

The head is the part of ``<head>+<tail>@<base>`` before the plus. Mail
clients and mail providers treat it as the mailbox, and the tail as an
alias. Inbound mail now routes on the head alone.

* An org that holds several hail-mail rows keeps one: the row with the most
  emails, then the oldest. Its ``emails`` and ``webhook_deliveries`` rows move
  to the kept row, and the other rows are deleted.
* Rows of different orgs that share a head are fixed the same way. The row
  with the most emails keeps the head, then the oldest. Each other row gets
  the new head ``<first 11 chars>-<8 hex chars>`` and a matching ``domain``.
* Partial unique indexes on the head and on the org replace
  ``email_domains_hail_mail_prefix_uq``: a unique head makes every (head,
  tail) pair unique, so nothing needs the old index.
* ``retired_hail_mail_addresses`` records every hail-mail address an org gives
  up. Triggers write it, so every writer is covered, including the rewrites
  above. A head retired by one org cannot be taken by another. Inbound ingest
  drops mail to an exact address another org retired.
* The same trigger refuses a role name (for example ``admin``) as a NEW head.
  A CHECK cannot do this, even NOT VALID: it runs on every update of a row, so
  an older row with such a head could not change its other columns.

Tables checked for retired addresses: ``email_domains`` holds the live address
of an org and one row per org by rule, so it cannot also hold old ones.
``email_forward_targets`` holds forward destinations, not Hail addresses.
``suppressions`` holds do-not-contact recipients. ``audit_log`` is a
best-effort trail with no rule attached. None fits, so this adds a table.

Downgrade removes the table, triggers and new indexes, and restores the old
index. It cannot restore a deleted row or a rewritten address: the old address
stops receiving mail at upgrade.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

# Copied here so this migration never changes when the code does. A test
# checks these match hailhq.core.models.
GUARD_FUNCTION = """
CREATE OR REPLACE FUNCTION email_domains_guard_hail_mail_head() RETURNS trigger AS $$
BEGIN
    IF NEW.kind <> 'hail_mail'
       OR (TG_OP = 'UPDATE'
           AND NEW.local_prefix_user IS NOT DISTINCT FROM OLD.local_prefix_user) THEN
        RETURN NEW;
    END IF;
    IF NEW.local_prefix_user = ANY (ARRAY[
           'abuse', 'admin', 'administrator', 'ftp', 'hostmaster',
           'info', 'is', 'it', 'mailer-daemon', 'marketing', 'mis',
           'news', 'noc', 'postmaster', 'root', 'sales', 'security',
           'ssl-admin', 'ssl-administrator', 'ssladmin',
           'ssladministrator', 'sslwebmaster', 'support', 'sysadmin',
           'usenet', 'uucp', 'webmaster', 'www'
       ]) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'hail-mail user prefix ' || NEW.local_prefix_user || ' is reserved';
    END IF;
    IF EXISTS (
           SELECT 1 FROM retired_hail_mail_addresses r
            WHERE r.local_prefix_user = NEW.local_prefix_user
              AND r.organization_id <> NEW.organization_id)
       AND NOT EXISTS (
           SELECT 1 FROM retired_hail_mail_addresses r
            WHERE r.local_prefix_user = NEW.local_prefix_user
              AND r.organization_id = NEW.organization_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23505',
            MESSAGE = 'hail-mail user prefix ' || NEW.local_prefix_user || ' is retired';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
GUARD_TRIGGER = """
CREATE OR REPLACE TRIGGER email_domains_guard_hail_mail_head
BEFORE INSERT OR UPDATE ON email_domains
FOR EACH ROW EXECUTE FUNCTION email_domains_guard_hail_mail_head()
"""
RETIRE_FUNCTION = """
CREATE OR REPLACE FUNCTION email_domains_retire_hail_mail() RETURNS trigger AS $$
BEGIN
    IF OLD.kind = 'hail_mail'
       AND (TG_OP = 'DELETE'
            OR NEW.local_prefix_user IS DISTINCT FROM OLD.local_prefix_user) THEN
        INSERT INTO retired_hail_mail_addresses
            (local_prefix_user, local_prefix_org, organization_id)
        VALUES (OLD.local_prefix_user, OLD.local_prefix_org, OLD.organization_id)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql
"""
RETIRE_TRIGGER = """
CREATE OR REPLACE TRIGGER email_domains_retire_hail_mail
AFTER UPDATE OR DELETE ON email_domains
FOR EACH ROW EXECUTE FUNCTION email_domains_retire_hail_mail()
"""


def upgrade() -> None:
    # A row written between the rewrites and the indexes would fail the deploy.
    op.execute("LOCK TABLE email_domains IN SHARE ROW EXCLUSIVE MODE")
    op.create_table(
        "retired_hail_mail_addresses",
        sa.Column("local_prefix_user", sa.Text(), primary_key=True),
        sa.Column("local_prefix_org", sa.Text(), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "retired_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # Before the rewrites, so they record the old addresses.
    op.execute(GUARD_FUNCTION)
    op.execute(GUARD_TRIGGER)
    op.execute(RETIRE_FUNCTION)
    op.execute(RETIRE_TRIGGER)

    op.execute("""
        CREATE TEMP TABLE hail_mail_merge ON COMMIT DROP AS
        SELECT d.id, first_value(d.id) OVER (
                   PARTITION BY d.organization_id
                   ORDER BY (
                       SELECT count(*) FROM emails e WHERE e.email_domain_id = d.id
                   ) DESC, d.created_at, d.id
               ) AS keeper
        FROM email_domains d
        WHERE d.kind = 'hail_mail'
        """)
    op.execute(
        "UPDATE emails e SET email_domain_id = m.keeper FROM hail_mail_merge m "
        "WHERE e.email_domain_id = m.id AND m.id <> m.keeper"
    )
    op.execute(
        "UPDATE webhook_deliveries w SET email_domain_id = m.keeper "
        "FROM hail_mail_merge m "
        "WHERE w.email_domain_id = m.id AND m.id <> m.keeper"
    )
    op.execute(
        "DELETE FROM email_domains d USING hail_mail_merge m "
        "WHERE d.id = m.id AND m.id <> m.keeper"
    )
    op.execute("DROP TABLE hail_mail_merge")

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
    op.execute(
        "CREATE UNIQUE INDEX email_domains_hail_mail_org_uq "
        "ON email_domains (organization_id) WHERE kind = 'hail_mail'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX email_domains_hail_mail_org_uq")
    op.execute("DROP INDEX email_domains_hail_mail_head_uq")
    op.execute(
        "CREATE UNIQUE INDEX email_domains_hail_mail_prefix_uq "
        "ON email_domains (local_prefix_user, local_prefix_org) "
        "WHERE kind = 'hail_mail'"
    )
    op.execute("DROP TRIGGER email_domains_retire_hail_mail ON email_domains")
    op.execute("DROP TRIGGER email_domains_guard_hail_mail_head ON email_domains")
    op.execute("DROP FUNCTION email_domains_retire_hail_mail()")
    op.execute("DROP FUNCTION email_domains_guard_hail_mail_head()")
    op.drop_table("retired_hail_mail_addresses")
