"""audit_log.api_key_id: text → uuid (drop "shared" string sentinel)

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14

Originally landed alongside 0003_call_end_reason_enum with the same
revision id, which made alembic silently drop one from history and skip
the audit_log conversion on fresh databases. Renumbered to 0004
(call_end_reason kept 0003 because it's the bigger schema change). The
two are independent — they touch different columns and can apply in
either order.

The body is wrapped in a column-type guard because some databases
already received the conversion under the old shared-0003 revision id
(both migrations ran, only one alembic_version row was kept). The guard
makes this migration a no-op on those DBs while still doing the work on
fresh installs where ``api_key_id`` is still TEXT.

The "shared" string sentinel for self-host HAIL_API_KEY requests is gone;
``api_key_id`` is now NULL on that path and a proper UUID otherwise. The
column was TEXT only to fit the sentinel — converting to UUID now that the
sentinel is dead. Existing rows with ``api_key_id = 'shared'`` are
backfilled to NULL.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Idempotent body: only run the backfill + ALTER if the column is still
# TEXT. Databases that already received the conversion under the old
# duplicate-0003 revision id stay untouched. The ``data_type`` lookup
# against ``information_schema`` returns ``text`` or ``uuid`` so the
# string compare is exact.
UPGRADE = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name   = 'audit_log'
       AND column_name  = 'api_key_id'
       AND data_type    = 'text'
  ) THEN
    UPDATE audit_log SET api_key_id = NULL WHERE api_key_id = 'shared';

    ALTER TABLE audit_log
      ALTER COLUMN api_key_id TYPE uuid USING api_key_id::uuid;
  END IF;
END $$;
"""


DOWNGRADE = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name   = 'audit_log'
       AND column_name  = 'api_key_id'
       AND data_type    = 'uuid'
  ) THEN
    ALTER TABLE audit_log
      ALTER COLUMN api_key_id TYPE text USING api_key_id::text;
  END IF;
END $$;

-- We don't re-introduce the 'shared' sentinel on downgrade — operators who
-- need the sentinel back will have to write it themselves.
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
