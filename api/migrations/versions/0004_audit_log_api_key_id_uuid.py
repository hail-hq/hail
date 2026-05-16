"""audit_log.api_key_id: text → uuid (drop "shared" string sentinel)

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14

The "shared" string sentinel for self-host HAIL_API_KEY requests is gone;
``api_key_id`` is now NULL on that path and a proper UUID otherwise. The
column was TEXT only to fit the sentinel — converting to UUID now that the
sentinel is dead. Existing rows with ``api_key_id = 'shared'`` are
backfilled to NULL.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE = """
-- Backfill the sentinel string to NULL before the type change, otherwise
-- the USING cast below would explode on 'shared'.
UPDATE audit_log SET api_key_id = NULL WHERE api_key_id = 'shared';

ALTER TABLE audit_log
  ALTER COLUMN api_key_id TYPE uuid USING api_key_id::uuid;
"""


DOWNGRADE = """
ALTER TABLE audit_log
  ALTER COLUMN api_key_id TYPE text USING api_key_id::text;

-- We don't re-introduce the 'shared' sentinel on downgrade — operators who
-- need the sentinel back will have to write it themselves.
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
