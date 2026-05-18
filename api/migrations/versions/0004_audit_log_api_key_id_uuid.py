"""convergence: audit_log api_key_id uuid + call_end_reason backfill

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14

Recovery migration covering the duplicate-0003 incident. Two migrations
originally claimed the slot:

  * ``0003_audit_log_api_key_id_uuid`` (landed in 9b0bcea, May 15)
  * ``0003_call_end_reason_enum`` (landed in deb3692, May 16)

Alembic silently dropped one from history depending on filesystem order,
so any DB that ran ``upgrade head`` lives in one of three shapes:

  A. Fresh after deb3692 — call_end_reason applied, audit_log conversion
     was the one that got dropped. Visible as audit_log.api_key_id=TEXT
     and call_end_reason ENUM present.
  B. Dev DB upgraded between 9b0bcea and deb3692 — audit_log converted,
     call_end_reason never ran. Visible as audit_log.api_key_id=UUID and
     **no** call_end_reason ENUM.
  C. Post-conversion DB (rare race) — both applied. Both guards no-op.

This file converges every shape onto the same target schema by guarding
each change with an information_schema / pg_type / pg_constraint probe.
The audit_log conversion drops the "shared" string sentinel (``api_key_id``
is NULL for HAIL_API_KEY callers now, a proper UUID otherwise).
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE = """
-- 1. audit_log.api_key_id: text → uuid (drop "shared" sentinel).
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

-- 2. call_end_reason ENUM + column ALTER, for State B DBs that missed
--    the original 0003_call_end_reason_enum. CREATE TYPE / ALTER are
--    guarded independently so a partially-applied state still converges.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'call_end_reason') THEN
    CREATE TYPE call_end_reason AS ENUM (
      'normal_hangup',
      'soft_cap_reached',
      'user_unavailable',
      'user_rejected',
      'sip_trunk_failure',
      'connection_timeout',
      'media_failure',
      'room_create_failed',
      'agent_dispatch_failed',
      'sip_participant_failed',
      'agent_error',
      'worker_shutdown',
      'sweeper_timeout',
      'unknown'
    );
  END IF;

  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name   = 'calls'
       AND column_name  = 'end_reason'
       AND data_type    = 'text'
  ) THEN
    UPDATE calls
       SET end_reason = 'unknown'
     WHERE end_reason IS NULL
       AND status IN ('completed','failed','busy','no_answer','canceled');

    ALTER TABLE calls
      ALTER COLUMN end_reason TYPE call_end_reason
      USING (
        CASE
          WHEN end_reason IS NULL THEN NULL
          WHEN end_reason = 'normal_hangup'                  THEN 'normal_hangup'::call_end_reason
          WHEN end_reason = 'soft_cap_reached'               THEN 'soft_cap_reached'::call_end_reason
          WHEN end_reason = 'user_unavailable'               THEN 'user_unavailable'::call_end_reason
          WHEN end_reason = 'user_rejected'                  THEN 'user_rejected'::call_end_reason
          WHEN end_reason = 'sip_trunk_failure'              THEN 'sip_trunk_failure'::call_end_reason
          WHEN end_reason = 'connection_timeout'             THEN 'connection_timeout'::call_end_reason
          WHEN end_reason = 'media_failure'                  THEN 'media_failure'::call_end_reason
          WHEN end_reason = 'livekit_room_create_failed'     THEN 'room_create_failed'::call_end_reason
          WHEN end_reason = 'livekit_agent_dispatch_failed'  THEN 'agent_dispatch_failed'::call_end_reason
          WHEN end_reason = 'livekit_sip_participant_failed' THEN 'sip_participant_failed'::call_end_reason
          WHEN end_reason = 'room_create_failed'             THEN 'room_create_failed'::call_end_reason
          WHEN end_reason = 'agent_dispatch_failed'          THEN 'agent_dispatch_failed'::call_end_reason
          WHEN end_reason = 'sip_participant_failed'         THEN 'sip_participant_failed'::call_end_reason
          WHEN end_reason = 'agent_error'                    THEN 'agent_error'::call_end_reason
          WHEN end_reason = 'worker_shutdown'                THEN 'worker_shutdown'::call_end_reason
          WHEN end_reason = 'sweeper_timeout'                THEN 'sweeper_timeout'::call_end_reason
          WHEN end_reason = 'unknown'                        THEN 'unknown'::call_end_reason
          ELSE 'unknown'::call_end_reason
        END
      );
  END IF;
END $$;

-- 3. CHECK constraint on calls. Guarded by name so it lands exactly
--    once, regardless of which 0003 ran first.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conname  = 'calls_end_reason_when_terminal'
       AND conrelid = 'calls'::regclass
  ) THEN
    ALTER TABLE calls
      ADD CONSTRAINT calls_end_reason_when_terminal CHECK (
        status NOT IN ('completed','failed','busy','no_answer','canceled')
        OR end_reason IS NOT NULL
      );
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
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
