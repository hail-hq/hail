"""call_end_reason ENUM + always-populated end_reason

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-16

Two things land in this migration:

1. Promote `calls.end_reason` from free-form TEXT to a Postgres ENUM
   (`call_end_reason`) with the 14 well-defined values listed below. The
   ALTER uses a USING clause to coerce existing values (renames legacy
   `livekit_X_failed` strings to the new `X_failed` names) and backfills
   NULL rows on terminal calls to `'unknown'` so the upcoming CHECK
   constraint can pass.

2. Add a CHECK that requires `end_reason IS NOT NULL` whenever `status`
   is in the terminal set. Non-terminal rows (queued/dialing/ringing/
   in_progress) may keep end_reason NULL.

Categories of values:
  * happy path:      normal_hangup, soft_cap_reached
  * SIP outcomes:    user_unavailable, user_rejected
  * SIP failures:    sip_trunk_failure, connection_timeout, media_failure
  * dispatch fails:  room_create_failed, agent_dispatch_failed,
                     sip_participant_failed
  * voicebot fails:  agent_error, worker_shutdown
  * backstop:        sweeper_timeout
  * catch-all:       unknown
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE = """
-- 1. The enum type itself.
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

-- 2. Backfill any terminal rows that have NULL end_reason today so the CHECK
--    can pass. Done as TEXT updates BEFORE the column type changes.
UPDATE calls
   SET end_reason = 'unknown'
 WHERE end_reason IS NULL
   AND status IN ('completed','failed','busy','no_answer','canceled');

-- 3. ALTER the column type. The USING clause maps each known legacy string
--    onto its new enum value; anything we do not recognize falls through to
--    'unknown' so the conversion never aborts on unexpected data.
ALTER TABLE calls
  ALTER COLUMN end_reason TYPE call_end_reason
  USING (
    CASE
      WHEN end_reason IS NULL THEN NULL
      WHEN end_reason = 'normal_hangup'             THEN 'normal_hangup'::call_end_reason
      WHEN end_reason = 'soft_cap_reached'          THEN 'soft_cap_reached'::call_end_reason
      WHEN end_reason = 'user_unavailable'          THEN 'user_unavailable'::call_end_reason
      WHEN end_reason = 'user_rejected'             THEN 'user_rejected'::call_end_reason
      WHEN end_reason = 'sip_trunk_failure'         THEN 'sip_trunk_failure'::call_end_reason
      WHEN end_reason = 'connection_timeout'        THEN 'connection_timeout'::call_end_reason
      WHEN end_reason = 'media_failure'             THEN 'media_failure'::call_end_reason
      -- Legacy "livekit_<stage>_failed" → new "<stage>_failed".
      WHEN end_reason = 'livekit_room_create_failed'     THEN 'room_create_failed'::call_end_reason
      WHEN end_reason = 'livekit_agent_dispatch_failed'  THEN 'agent_dispatch_failed'::call_end_reason
      WHEN end_reason = 'livekit_sip_participant_failed' THEN 'sip_participant_failed'::call_end_reason
      WHEN end_reason = 'room_create_failed'        THEN 'room_create_failed'::call_end_reason
      WHEN end_reason = 'agent_dispatch_failed'     THEN 'agent_dispatch_failed'::call_end_reason
      WHEN end_reason = 'sip_participant_failed'    THEN 'sip_participant_failed'::call_end_reason
      WHEN end_reason = 'agent_error'               THEN 'agent_error'::call_end_reason
      WHEN end_reason = 'worker_shutdown'           THEN 'worker_shutdown'::call_end_reason
      WHEN end_reason = 'sweeper_timeout'           THEN 'sweeper_timeout'::call_end_reason
      WHEN end_reason = 'unknown'                   THEN 'unknown'::call_end_reason
      ELSE 'unknown'::call_end_reason
    END
  );

-- 4. Schema-level invariant: every terminal call carries an end_reason.
ALTER TABLE calls
  ADD CONSTRAINT calls_end_reason_when_terminal CHECK (
    status NOT IN ('completed','failed','busy','no_answer','canceled')
    OR end_reason IS NOT NULL
  );
"""


DOWNGRADE = """
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_end_reason_when_terminal;
ALTER TABLE calls ALTER COLUMN end_reason TYPE TEXT USING end_reason::text;
DROP TYPE IF EXISTS call_end_reason;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
