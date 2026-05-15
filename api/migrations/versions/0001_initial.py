"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-24

The `api_keys`, `organizations`, and `members` tables are owned by the
website's Better Auth backend (see hail-website/better-auth_migrations).
Columns here that reference those rows (audit_log.api_key_id,
organization_id everywhere) carry no foreign-key constraint so the two
migration histories don't need to coordinate.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- ACCOUNT CREDITS — append-only ledger keyed on organization.
-- Balance = SUM(amount_cents); credits positive, debits negative.
-- channel is the modality the debit/credit applies to (or 'credit' for
-- a money-in event like a top-up or refund).
-- Aggregate per-batch for high-volume channels (one row per email blast
-- with qty=N) so the table grows with batches, not with messages.
-- ============================================================
CREATE TABLE account_credits (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('credit','debit')),
  channel         TEXT NOT NULL CHECK (channel IN ('voice','sms','email','credit')),
  amount_cents    INTEGER NOT NULL,
  qty             INTEGER,
  ref             TEXT,
  source          TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (kind = 'credit' AND amount_cents > 0)
    OR (kind = 'debit' AND amount_cents < 0)
  )
);
CREATE INDEX idx_account_credits_org_created
  ON account_credits(organization_id, created_at DESC);
CREATE INDEX idx_account_credits_ref
  ON account_credits(ref) WHERE ref IS NOT NULL;

-- ============================================================
-- USAGE EVENTS — raw, channel-typed units. No money math.
-- Voicebot/SMS/email writers insert here at lifecycle end with the
-- bare consumption units (voice: duration_ms; sms: segments; email:
-- count). A rater in the website's private repo reads unpriced rows
-- (`priced_at IS NULL`) and writes the corresponding dollar debit
-- row to `account_credits`, then stamps `priced_at` on the source
-- row. Self-host operators never run the rater — usage_events
-- accumulates as a raw analytics primitive they can query directly.
-- ============================================================
CREATE TABLE usage_events (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  channel         TEXT NOT NULL CHECK (channel IN ('voice','sms','email')),
  units           INTEGER NOT NULL CHECK (units >= 0),
  ref             TEXT,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  priced_at       TIMESTAMPTZ
);
CREATE INDEX idx_usage_events_org_time
  ON usage_events(organization_id, occurred_at DESC);
-- Partial index drives the rater's "what hasn't been priced yet?" scan.
CREATE INDEX idx_usage_events_unpriced
  ON usage_events(occurred_at)
  WHERE priced_at IS NULL;

-- ============================================================
-- PHONE NUMBERS
-- ============================================================
CREATE TABLE phone_numbers (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id        UUID NOT NULL,
  e164                   TEXT NOT NULL UNIQUE,
  country_code           TEXT NOT NULL,
  number_type            TEXT NOT NULL CHECK (number_type IN ('local','mobile','toll_free')),
  capabilities           TEXT[] NOT NULL DEFAULT ARRAY['voice','sms'],
  provider               TEXT NOT NULL DEFAULT 'twilio',
  provider_resource_id   TEXT NOT NULL,
  provisioning_state     TEXT NOT NULL DEFAULT 'pending'
                         CHECK (provisioning_state IN ('pending','active','failed','released')),
  provisioning_metadata  JSONB NOT NULL DEFAULT '{}',
  acquired_at            TIMESTAMPTZ,
  released_at            TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_phone_numbers_org ON phone_numbers(organization_id);
CREATE INDEX idx_phone_numbers_state ON phone_numbers(provisioning_state)
  WHERE provisioning_state IN ('pending','failed');
CREATE TRIGGER phone_numbers_updated_at BEFORE UPDATE ON phone_numbers
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- CONVERSATIONS
-- Grouping of related comms for one task; external_id correlates
-- to the caller's own systems.
-- ============================================================
CREATE TABLE conversations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  external_id     TEXT,
  metadata        JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_conversations_org ON conversations(organization_id);
CREATE INDEX idx_conversations_external
  ON conversations(organization_id, external_id)
  WHERE external_id IS NOT NULL;
CREATE TRIGGER conversations_updated_at BEFORE UPDATE ON conversations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- CALLS
-- voice_config is snapshotted at dispatch so historical replays
-- reflect the exact config that ran.
-- ============================================================
CREATE TABLE calls (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id       UUID NOT NULL,
  conversation_id       UUID REFERENCES conversations(id) ON DELETE SET NULL,
  from_number_id        UUID NOT NULL REFERENCES phone_numbers(id),
  from_e164             TEXT NOT NULL,
  to_e164               TEXT NOT NULL,
  direction             TEXT NOT NULL DEFAULT 'outbound'
                        CHECK (direction IN ('outbound','inbound')),
  status                TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','dialing','ringing','in_progress',
                                          'completed','failed','busy','no_answer','canceled')),
  end_reason            TEXT,
  provider              TEXT NOT NULL DEFAULT 'twilio',
  provider_call_sid     TEXT UNIQUE,
  livekit_room          TEXT,
  voice_config          JSONB NOT NULL,
  initial_prompt        TEXT,
  transcript            JSONB,
  recording_s3_key      TEXT,
  recording_duration_ms INTEGER,
  cost_cents            INTEGER,
  requested_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at            TIMESTAMPTZ,
  answered_at           TIMESTAMPTZ,
  ended_at              TIMESTAMPTZ,
  metadata              JSONB NOT NULL DEFAULT '{}',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_calls_org_created ON calls(organization_id, created_at DESC);
CREATE INDEX idx_calls_status ON calls(status)
  WHERE status IN ('queued','dialing','ringing','in_progress');
CREATE INDEX idx_calls_conversation ON calls(conversation_id)
  WHERE conversation_id IS NOT NULL;
CREATE INDEX idx_calls_to ON calls(organization_id, to_e164);
CREATE TRIGGER calls_updated_at BEFORE UPDATE ON calls
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- CALL EVENTS — append-only state transitions and turn logs.
-- calls.status is a denormalization; this table is the source of truth.
-- ============================================================
CREATE TABLE call_events (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  payload     JSONB NOT NULL,
  occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_call_events_call ON call_events(call_id, occurred_at);

-- ============================================================
-- IDEMPOTENCY KEYS
-- Cached request → response for retry safety. GC via expires_at.
-- ============================================================
CREATE TABLE idempotency_keys (
  key              TEXT PRIMARY KEY,
  organization_id  UUID NOT NULL,
  request_hash     TEXT NOT NULL,
  response_status  INTEGER NOT NULL,
  response_body    JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours'
);
CREATE INDEX idx_idempotency_expires ON idempotency_keys(expires_at);

-- ============================================================
-- AUDIT LOG — append-only record of mutating API actions.
-- api_key_id and organization_id have no foreign keys — the upstream tables
-- live in a separate migration history owned by the website. Treat these
-- columns as informational lineage, not referential integrity. HAIL_API_KEY
-- (self-host) requests use the nil UUID for organization_id and write NULL
-- to api_key_id. (api_key_id was originally TEXT to hold a "shared" sentinel
-- string; migration 0003 backfills it to NULL and converts the column to UUID.)
-- ============================================================
CREATE TABLE audit_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL,
  api_key_id      TEXT,
  action          TEXT NOT NULL,
  resource_type   TEXT,
  resource_id     UUID,
  ip_address      INET,
  user_agent      TEXT,
  payload         JSONB,
  occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_org_time ON audit_log(organization_id, occurred_at DESC);
CREATE INDEX idx_audit_resource ON audit_log(resource_type, resource_id)
  WHERE resource_id IS NOT NULL;
"""


DOWNGRADE = """
DROP TABLE IF EXISTS
  audit_log,
  idempotency_keys,
  call_events,
  calls,
  conversations,
  phone_numbers,
  usage_events,
  account_credits
CASCADE;
DROP FUNCTION IF EXISTS set_updated_at() CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
