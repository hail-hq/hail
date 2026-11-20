"""Name the hail-mail prefix columns for what they hold: step A of 3.

A hail-mail address is ``<workspace>+<name>@<base>``. The part before the plus
names the workspace and is unique per deployment. The part after it is a free
name inside the workspace. The old columns had the names the wrong way round:
``local_prefix_user`` holds the workspace part and ``local_prefix_org`` holds
the name part.

This adds ``local_prefix_workspace`` and ``local_prefix_name`` to
``email_domains`` and to ``retired_hail_mail_addresses``, copies the old
values into them, and keeps both pairs equal with a trigger:

* A writer that sets only the old pair gets the new pair filled, and the
  other way round. The website still writes the old pair.
* On INSERT, when both pairs are given and differ, the new pair wins.
* On UPDATE, the pair that changed wins. When both changed, the new pair wins.

The trigger name sorts before every other trigger on these tables. Postgres
fires triggers of the same timing in name order, so the 0070 guard and the
retire trigger, which still read the old columns, see filled values.

The unique indexes, CHECK constraints, the primary key of
``retired_hail_mail_addresses`` and the 0070 trigger functions stay on the
old columns. A later step moves them to the new columns and drops the old
ones, once no writer uses the old names.

Downgrade drops the triggers, the function and the new columns. The old
columns still hold every value, so nothing is lost.
"""

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None

TABLES = ("email_domains", "retired_hail_mail_addresses")

# Copied here so this migration never changes when the code does. A test
# checks these match hailhq.core.models.
SYNC_FUNCTION = """
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
# "00" sorts before every other trigger name on the table.
SYNC_TRIGGERS = tuple(f"""
CREATE OR REPLACE TRIGGER {table}_00_sync_prefix_columns
BEFORE INSERT OR UPDATE ON {table}
FOR EACH ROW EXECUTE FUNCTION hail_mail_sync_prefix_columns()
""" for table in TABLES)


def upgrade() -> None:
    for table in TABLES:
        # Adding a column locks the table until commit, so no writer can add
        # a row between the copy and the trigger.
        op.add_column(table, sa.Column("local_prefix_workspace", sa.Text()))
        op.add_column(table, sa.Column("local_prefix_name", sa.Text()))
        op.execute(
            f"UPDATE {table} SET local_prefix_workspace = local_prefix_user, "
            "local_prefix_name = local_prefix_org "
            "WHERE local_prefix_user IS NOT NULL OR local_prefix_org IS NOT NULL"
        )
    op.execute(SYNC_FUNCTION)
    for statement in SYNC_TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_00_sync_prefix_columns ON {table}")
    op.execute("DROP FUNCTION hail_mail_sync_prefix_columns()")
    for table in TABLES:
        op.drop_column(table, "local_prefix_name")
        op.drop_column(table, "local_prefix_workspace")
