"""phone number pool: shared numbers + reservation tracking

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14

Pool numbers are owned by Hail (organization_id IS NULL, is_pool=TRUE) and
shared across orgs that have no provisioned number of their own. A row is
"in use" iff reserved_call_id IS NOT NULL — single source of truth, no
separate state enum or boolean. Quarantine is handled by flipping
provisioning_state to 'failed' or 'released' (already in the v1 schema).

calls.max_duration_seconds snapshots the effective max-call-duration at
insert time so the sweeper's backstop release (now() > requested_at +
max_duration + grace) can't release a live call after a config tweak.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE = """
-- 1. organization_id becomes nullable for pool numbers.
ALTER TABLE phone_numbers
  ALTER COLUMN organization_id DROP NOT NULL;

-- 2. Pool flag + reservation FK.
ALTER TABLE phone_numbers
  ADD COLUMN is_pool BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN reserved_call_id UUID
    REFERENCES calls(id) ON DELETE SET NULL;

-- 3. Schema-level invariant: pool rows have no owner, owned rows aren't pool.
ALTER TABLE phone_numbers
  ADD CONSTRAINT phone_numbers_pool_owner_xor
  CHECK (
    (is_pool = TRUE AND organization_id IS NULL)
    OR (is_pool = FALSE AND organization_id IS NOT NULL)
  );

-- 4. Hot-path claim: SELECT … ORDER BY random() LIMIT 1 FOR UPDATE SKIP LOCKED.
--    The partial WHERE clause is what filters down to claimable rows; the
--    index key (id) is just a required placeholder — random() can't be
--    answered from an index, so the planner sorts the filtered set.
CREATE INDEX idx_phone_numbers_pool_available
  ON phone_numbers(id)
  WHERE is_pool AND reserved_call_id IS NULL AND provisioning_state = 'active';

-- 5. Release helper looks rows up by reserved_call_id.
CREATE INDEX idx_phone_numbers_reserved_call
  ON phone_numbers(reserved_call_id)
  WHERE reserved_call_id IS NOT NULL;

-- 6. Snapshot the effective max-call-duration at insert time so config drift
--    can't retroactively shorten the sweeper's backstop window.
ALTER TABLE calls
  ADD COLUMN max_duration_seconds INTEGER;
"""


DOWNGRADE = """
ALTER TABLE calls
  DROP COLUMN IF EXISTS max_duration_seconds;

DROP INDEX IF EXISTS idx_phone_numbers_reserved_call;
DROP INDEX IF EXISTS idx_phone_numbers_pool_available;

ALTER TABLE phone_numbers
  DROP CONSTRAINT IF EXISTS phone_numbers_pool_owner_xor,
  DROP COLUMN IF EXISTS reserved_call_id,
  DROP COLUMN IF EXISTS is_pool;

-- Before re-tightening the NOT NULL, any pool rows (organization_id NULL)
-- must be gone. The DROP COLUMN above removed is_pool but rows are still
-- there if pool numbers were inserted. Operators downgrading must clear
-- those rows manually first; we leave the NOT NULL off to avoid blowing up
-- on a downgrade against a populated pool.
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
