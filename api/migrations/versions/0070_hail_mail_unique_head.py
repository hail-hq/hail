"""One hail-mail address per org, one head per deployment, retired addresses.

The head is the part of ``<head>+<tail>@<base>`` before the plus. Mail
clients and mail providers treat it as the mailbox, and the tail as an
alias. Inbound mail now routes on the head alone.

* An org that holds several hail-mail rows keeps one: the row with the most
  emails, then the oldest. The kept row takes the union of ``forward_to``,
  turns ``inbound_enabled`` on if any row had it, and keeps its own
  ``forward_rate_per_hour``. The ``emails`` and ``webhook_deliveries`` rows of
  the others move to the kept row, and the others are deleted.
* Rows of different orgs that share a head are fixed the same way. The row
  with the most emails keeps the head, then the oldest. Each other row gets
  the new head ``<first 11 chars>-<8 hex chars>`` and a matching ``domain``.
* Partial unique indexes on the head and on the org replace
  ``email_domains_hail_mail_prefix_uq``: a unique head makes every (head,
  tail) pair unique, so nothing needs the old index.
* ``retired_hail_mail_addresses`` records every hail-mail address an org gives
  up. Triggers write it, so every writer is covered, including the rewrites
  above. Only the org that gave a head up last may take it back. Inbound
  ingest drops mail to an exact address another org retired.
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
DECLARE
    newest_org uuid;
    head_changed boolean;
BEGIN
    -- Claiming a head and releasing it must run one after the other. Otherwise
    -- a claim can read no retirement record, then commit just after the release
    -- and take the head. The lock is taken here, before the row changes: taken
    -- only after the change, a release and a claim would deadlock on the unique
    -- index. Heads are locked in sorted order for the same reason.
    IF TG_OP = 'DELETE' THEN
        IF OLD.kind = 'hail_mail' THEN
            PERFORM pg_advisory_xact_lock(hashtext(OLD.local_prefix_user));
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.kind <> 'hail_mail'
       OR (TG_OP = 'UPDATE'
           AND NEW.local_prefix_user IS NOT DISTINCT FROM OLD.local_prefix_user
           AND NEW.local_prefix_org IS NOT DISTINCT FROM OLD.local_prefix_org) THEN
        RETURN NEW;
    END IF;
    head_changed := TG_OP = 'INSERT'
        OR NEW.local_prefix_user IS DISTINCT FROM OLD.local_prefix_user;
    IF head_changed AND NEW.local_prefix_user = ANY (ARRAY[
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
    PERFORM pg_advisory_xact_lock(hashtext(head))
       FROM (SELECT DISTINCT head
               FROM unnest(ARRAY[OLD.local_prefix_user, NEW.local_prefix_user]) AS head
              WHERE head IS NOT NULL
              ORDER BY head) AS heads;
    -- Only the org that gave the head up last may take it back.
    IF head_changed THEN
        SELECT r.organization_id INTO newest_org
          FROM retired_hail_mail_addresses r
         WHERE r.local_prefix_user = NEW.local_prefix_user
         ORDER BY r.retired_at DESC, r.organization_id
         LIMIT 1;
        IF FOUND AND newest_org <> NEW.organization_id THEN
            RAISE EXCEPTION USING ERRCODE = '23505',
                MESSAGE = 'hail-mail user prefix ' || NEW.local_prefix_user || ' is retired';
        END IF;
    END IF;
    -- A new tail must not claim the exact address another org gave up: ingest
    -- would then hold mail that was meant for that org.
    IF EXISTS (
           SELECT 1 FROM retired_hail_mail_addresses r
            WHERE r.local_prefix_user = NEW.local_prefix_user
              AND r.local_prefix_org = NEW.local_prefix_org
              AND r.organization_id <> NEW.organization_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23505',
            MESSAGE = 'hail-mail address ' || NEW.local_prefix_user || '+'
                || NEW.local_prefix_org || ' is retired';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
GUARD_TRIGGER = """
CREATE OR REPLACE TRIGGER email_domains_guard_hail_mail_head
BEFORE INSERT OR UPDATE OR DELETE ON email_domains
FOR EACH ROW EXECUTE FUNCTION email_domains_guard_hail_mail_head()
"""
RETIRE_FUNCTION = """
CREATE OR REPLACE FUNCTION email_domains_retire_hail_mail() RETURNS trigger AS $$
BEGIN
    IF OLD.kind = 'hail_mail'
       AND (TG_OP = 'DELETE'
            OR NEW.local_prefix_user IS DISTINCT FROM OLD.local_prefix_user) THEN
        -- The guard already holds this lock on any normal path. It stays here
        -- so a release can never run unserialized, even with the guard disabled.
        PERFORM pg_advisory_xact_lock(hashtext(OLD.local_prefix_user));
        -- clock_timestamp, not now(): two rewrites in one transaction need an
        -- order, and the newest record decides who may take the head back.
        INSERT INTO retired_hail_mail_addresses
            (local_prefix_user, local_prefix_org, organization_id, retired_at)
        VALUES (OLD.local_prefix_user, OLD.local_prefix_org, OLD.organization_id,
                clock_timestamp())
        ON CONFLICT (local_prefix_user, local_prefix_org, organization_id)
        DO UPDATE SET retired_at = EXCLUDED.retired_at;
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


# The row with the most emails stays. Used by the merge and by the rewrite.
EMAIL_COUNTS = """
email_counts AS (
    SELECT email_domain_id, count(*) AS n
      FROM emails
     WHERE email_domain_id IS NOT NULL
     GROUP BY email_domain_id
)
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

    # Only a merge moves rows in these tables, so only a merge locks them: an
    # old container must not insert a row for a domain that is about to go.
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM email_domains
                 WHERE kind = 'hail_mail'
                 GROUP BY organization_id
                HAVING count(*) > 1
            ) THEN
                LOCK TABLE emails, webhook_deliveries IN SHARE ROW EXCLUSIVE MODE;
            END IF;
        END
        $$
        """)
    op.execute(f"""
        CREATE TEMP TABLE hail_mail_merge ON COMMIT DROP AS
        WITH {EMAIL_COUNTS}
        SELECT d.id, first_value(d.id) OVER (
                   PARTITION BY d.organization_id
                   ORDER BY coalesce(c.n, 0) DESC, d.created_at, d.id
               ) AS keeper
        FROM email_domains d
        LEFT JOIN email_counts c ON c.email_domain_id = d.id
        WHERE d.kind = 'hail_mail'
        """)
    op.execute("""
        UPDATE email_domains k
           SET forward_to = s.forward_to,
               inbound_enabled = s.inbound_enabled,
               updated_at = now()
          FROM (
              SELECT m.keeper,
                     bool_or(d.inbound_enabled) AS inbound_enabled,
                     array_agg(DISTINCT t.address) FILTER (
                         WHERE t.address IS NOT NULL
                     ) AS forward_to
                FROM hail_mail_merge m
                JOIN email_domains d ON d.id = m.id
                LEFT JOIN LATERAL unnest(d.forward_to) AS t(address) ON true
               GROUP BY m.keeper
              HAVING count(DISTINCT m.id) > 1
          ) s
         WHERE k.id = s.keeper
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

    op.execute(f"""
        WITH {EMAIL_COUNTS},
        ranked AS (
            SELECT d.id, row_number() OVER (
                PARTITION BY d.local_prefix_user
                ORDER BY coalesce(c.n, 0) DESC, d.created_at, d.id
            ) AS rn
            FROM email_domains d
            LEFT JOIN email_counts c ON c.email_domain_id = d.id
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
