"""Drop the old hail-mail prefix columns: step C of 3.

Step A added ``local_prefix_workspace`` (the part before the ``+``) and
``local_prefix_name`` (the part after it) next to the old
``local_prefix_user`` and ``local_prefix_org``, and kept both pairs equal with
a sync trigger. Step B moved every writer to the new pair.

This moves every rule from the old pair to the new pair and drops the old
pair, on ``email_domains`` and ``retired_hail_mail_addresses``:

* It first checks that both pairs hold the same values on every row, and
  stops if one does not.
* The unique head index, the two CHECK constraints and the retired table's
  primary key are rebuilt on the new columns, with the same rules. The
  reserved-name CHECK stays NOT VALID, as before: Postgres skips the check
  of existing rows, but still checks each row an INSERT or UPDATE writes.
* The guard and retire functions read the new columns. Their messages say
  "workspace prefix". Their error codes do not change.
* The sync trigger and its function are dropped.
* The plain indexes that step A added are dropped: the new unique index and
  the new primary key cover the same lookups.

Running containers read only the new pair, so they keep working while this
runs. Downgrade adds the old pair back, copies the values, and restores the
step A shape.
"""

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None

TABLES = ("email_domains", "retired_hail_mail_addresses")

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
            PERFORM pg_advisory_xact_lock(hashtext(OLD.local_prefix_workspace));
        END IF;
        RETURN OLD;
    END IF;
    IF NEW.kind <> 'hail_mail'
       OR (TG_OP = 'UPDATE'
           AND NEW.local_prefix_workspace IS NOT DISTINCT FROM OLD.local_prefix_workspace
           AND NEW.local_prefix_name IS NOT DISTINCT FROM OLD.local_prefix_name) THEN
        RETURN NEW;
    END IF;
    head_changed := TG_OP = 'INSERT'
        OR NEW.local_prefix_workspace IS DISTINCT FROM OLD.local_prefix_workspace;
    IF head_changed AND NEW.local_prefix_workspace = ANY (ARRAY[
           'abuse', 'admin', 'administrator', 'ftp', 'hostmaster',
           'info', 'is', 'it', 'mailer-daemon', 'marketing', 'mis',
           'news', 'noc', 'postmaster', 'root', 'sales', 'security',
           'ssl-admin', 'ssl-administrator', 'ssladmin',
           'ssladministrator', 'sslwebmaster', 'support', 'sysadmin',
           'usenet', 'uucp', 'webmaster', 'www'
       ]) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'hail-mail workspace prefix ' || NEW.local_prefix_workspace || ' is reserved';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext(head))
       FROM (SELECT DISTINCT head
               FROM unnest(ARRAY[OLD.local_prefix_workspace, NEW.local_prefix_workspace]) AS head
              WHERE head IS NOT NULL
              ORDER BY head) AS heads;
    -- Only the org that gave the head up last may take it back.
    IF head_changed THEN
        SELECT r.organization_id INTO newest_org
          FROM retired_hail_mail_addresses r
         WHERE r.local_prefix_workspace = NEW.local_prefix_workspace
         ORDER BY r.retired_at DESC, r.organization_id
         LIMIT 1;
        IF FOUND AND newest_org <> NEW.organization_id THEN
            RAISE EXCEPTION USING ERRCODE = '23505',
                MESSAGE = 'hail-mail workspace prefix ' || NEW.local_prefix_workspace || ' is retired';
        END IF;
    END IF;
    -- A new tail must not claim the exact address another org gave up: ingest
    -- would then hold mail that was meant for that org.
    IF EXISTS (
           SELECT 1 FROM retired_hail_mail_addresses r
            WHERE r.local_prefix_workspace = NEW.local_prefix_workspace
              AND r.local_prefix_name = NEW.local_prefix_name
              AND r.organization_id <> NEW.organization_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23505',
            MESSAGE = 'hail-mail address ' || NEW.local_prefix_workspace || '+'
                || NEW.local_prefix_name || ' is retired';
    END IF;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
RETIRE_FUNCTION = """
CREATE OR REPLACE FUNCTION email_domains_retire_hail_mail() RETURNS trigger AS $$
BEGIN
    IF OLD.kind = 'hail_mail'
       AND (TG_OP = 'DELETE'
            OR NEW.local_prefix_workspace IS DISTINCT FROM OLD.local_prefix_workspace) THEN
        -- The guard already holds this lock on any normal path. It stays here
        -- so a release can never run unserialized, even with the guard disabled.
        PERFORM pg_advisory_xact_lock(hashtext(OLD.local_prefix_workspace));
        -- clock_timestamp, not now(): two rewrites in one transaction need an
        -- order, and the newest record decides who may take the head back.
        INSERT INTO retired_hail_mail_addresses
            (local_prefix_workspace, local_prefix_name, organization_id, retired_at)
        VALUES (OLD.local_prefix_workspace, OLD.local_prefix_name, OLD.organization_id,
                clock_timestamp())
        ON CONFLICT (local_prefix_workspace, local_prefix_name, organization_id)
        DO UPDATE SET retired_at = EXCLUDED.retired_at;
    END IF;
    RETURN NULL;
END
$$ LANGUAGE plpgsql
"""

# The step A versions, for downgrade.
OLD_GUARD_FUNCTION = """
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
OLD_RETIRE_FUNCTION = """
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
OLD_SYNC_FUNCTION = """
CREATE OR REPLACE FUNCTION hail_mail_sync_prefix_columns() RETURNS trigger AS $$
BEGIN
    -- local_prefix_workspace = local_prefix_user, local_prefix_name =
    -- local_prefix_org. The new pair wins a conflict.
    IF TG_OP = 'INSERT' THEN
        NEW.local_prefix_workspace :=
            COALESCE(NEW.local_prefix_workspace, NEW.local_prefix_user);
        NEW.local_prefix_name := COALESCE(NEW.local_prefix_name, NEW.local_prefix_org);
    ELSE
        IF NEW.local_prefix_workspace IS NOT DISTINCT FROM OLD.local_prefix_workspace THEN
            NEW.local_prefix_workspace := NEW.local_prefix_user;
        END IF;
        IF NEW.local_prefix_name IS NOT DISTINCT FROM OLD.local_prefix_name THEN
            NEW.local_prefix_name := NEW.local_prefix_org;
        END IF;
    END IF;
    NEW.local_prefix_user := NEW.local_prefix_workspace;
    NEW.local_prefix_org := NEW.local_prefix_name;
    RETURN NEW;
END
$$ LANGUAGE plpgsql
"""
OLD_SYNC_TRIGGERS = tuple(f"""
CREATE OR REPLACE TRIGGER {table}_00_sync_prefix_columns
BEFORE INSERT OR UPDATE ON {table}
FOR EACH ROW EXECUTE FUNCTION hail_mail_sync_prefix_columns()
""" for table in TABLES)

PAIRS_DIFFER = """
DO $$
BEGIN
    IF EXISTS (
           SELECT 1 FROM email_domains
            WHERE local_prefix_workspace IS DISTINCT FROM local_prefix_user
               OR local_prefix_name IS DISTINCT FROM local_prefix_org)
       OR EXISTS (
           SELECT 1 FROM retired_hail_mail_addresses
            WHERE local_prefix_workspace IS DISTINCT FROM local_prefix_user
               OR local_prefix_name IS DISTINCT FROM local_prefix_org) THEN
        RAISE EXCEPTION 'hail-mail prefix columns differ; fix the rows first';
    END IF;
END
$$
"""


def _prefix_kind_check(head: str, tail: str) -> None:
    op.create_check_constraint(
        "email_domains_prefix_kind_consistency",
        "email_domains",
        f"(kind = 'hail_mail' AND {head} IS NOT NULL AND {tail} IS NOT NULL) "
        f"OR (kind = 'custom' AND {head} IS NULL AND {tail} IS NULL)",
    )


def _reserved_check(name: str, head: str) -> None:
    # NOT VALID, as before: skips the check of existing rows only.
    op.execute(
        f"ALTER TABLE email_domains ADD CONSTRAINT {name} CHECK ("
        f"{head} IS NULL OR {head} NOT IN ('noreply','forwarder')) NOT VALID"
    )


def _head_index(head: str) -> None:
    op.create_index(
        "email_domains_hail_mail_head_uq",
        "email_domains",
        [head],
        unique=True,
        postgresql_where=sa.text("kind = 'hail_mail'"),
    )


def _retired_pkey(head: str, tail: str) -> None:
    op.create_primary_key(
        "retired_hail_mail_addresses_pkey",
        "retired_hail_mail_addresses",
        [head, tail, "organization_id"],
    )


def _updated_at_trigger(action: str) -> None:
    # A schema built from the models (tests) has no updated_at trigger.
    op.execute(f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_trigger
                WHERE tgrelid = 'email_domains'::regclass
                  AND tgname = 'email_domains_updated_at') THEN
        ALTER TABLE email_domains {action} TRIGGER email_domains_updated_at;
    END IF;
END
$$
""")


def _lock() -> None:
    # One lock for both tables, so no writer gets in between the steps.
    op.execute(
        "LOCK TABLE email_domains, retired_hail_mail_addresses IN ACCESS EXCLUSIVE MODE"
    )


def upgrade() -> None:
    _lock()
    op.execute(PAIRS_DIFFER)
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_00_sync_prefix_columns ON {table}")
    op.execute("DROP FUNCTION hail_mail_sync_prefix_columns()")

    op.drop_constraint("email_domains_prefix_kind_consistency", "email_domains")
    _prefix_kind_check("local_prefix_workspace", "local_prefix_name")
    op.drop_constraint("email_domains_user_prefix_not_reserved", "email_domains")
    _reserved_check(
        "email_domains_workspace_prefix_not_reserved", "local_prefix_workspace"
    )
    op.drop_index("email_domains_hail_mail_head_uq", table_name="email_domains")
    _head_index("local_prefix_workspace")
    op.drop_index("email_domains_hail_mail_workspace_idx", table_name="email_domains")

    for column in ("local_prefix_workspace", "local_prefix_name"):
        op.alter_column("retired_hail_mail_addresses", column, nullable=False)
    op.drop_constraint(
        "retired_hail_mail_addresses_pkey", "retired_hail_mail_addresses"
    )
    _retired_pkey("local_prefix_workspace", "local_prefix_name")
    op.drop_index(
        "retired_hail_mail_addresses_workspace_idx",
        table_name="retired_hail_mail_addresses",
    )

    op.execute(GUARD_FUNCTION)
    op.execute(RETIRE_FUNCTION)
    for table in TABLES:
        op.drop_column(table, "local_prefix_user")
        op.drop_column(table, "local_prefix_org")


def downgrade() -> None:
    _lock()
    # The copy is not an edit: keep each row's updated_at.
    _updated_at_trigger("DISABLE")
    for table in TABLES:
        op.add_column(table, sa.Column("local_prefix_user", sa.Text()))
        op.add_column(table, sa.Column("local_prefix_org", sa.Text()))
        op.execute(
            f"UPDATE {table} SET local_prefix_user = local_prefix_workspace, "
            "local_prefix_org = local_prefix_name "
            "WHERE local_prefix_workspace IS NOT NULL "
            "OR local_prefix_name IS NOT NULL"
        )
    _updated_at_trigger("ENABLE")

    op.execute(OLD_GUARD_FUNCTION)
    op.execute(OLD_RETIRE_FUNCTION)

    op.create_index(
        "retired_hail_mail_addresses_workspace_idx",
        "retired_hail_mail_addresses",
        ["local_prefix_workspace", "local_prefix_name"],
    )
    op.drop_constraint(
        "retired_hail_mail_addresses_pkey", "retired_hail_mail_addresses"
    )
    _retired_pkey("local_prefix_user", "local_prefix_org")
    for column in ("local_prefix_workspace", "local_prefix_name"):
        op.alter_column("retired_hail_mail_addresses", column, nullable=True)

    op.create_index(
        "email_domains_hail_mail_workspace_idx",
        "email_domains",
        ["local_prefix_workspace"],
        postgresql_where=sa.text("kind = 'hail_mail'"),
    )
    op.drop_index("email_domains_hail_mail_head_uq", table_name="email_domains")
    _head_index("local_prefix_user")
    op.drop_constraint("email_domains_workspace_prefix_not_reserved", "email_domains")
    _reserved_check("email_domains_user_prefix_not_reserved", "local_prefix_user")
    op.drop_constraint("email_domains_prefix_kind_consistency", "email_domains")
    _prefix_kind_check("local_prefix_user", "local_prefix_org")

    op.execute(OLD_SYNC_FUNCTION)
    for statement in OLD_SYNC_TRIGGERS:
        op.execute(statement)
