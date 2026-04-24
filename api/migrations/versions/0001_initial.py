"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-04-24

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
-- ORGANIZATIONS — tenant root
-- ============================================================
CREATE TABLE organizations (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL,
  slug       TEXT NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER organizations_updated_at BEFORE UPDATE ON organizations
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- API KEYS
-- key_hash is SHA-256 of the full key; key_prefix is safe to display.
-- ============================================================
CREATE TABLE api_keys (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  key_prefix      TEXT NOT NULL,
  key_hash        TEXT NOT NULL UNIQUE,
  scopes          TEXT[] NOT NULL DEFAULT ARRAY['*'],
  last_used_at    TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_api_keys_org ON api_keys(organization_id);
CREATE INDEX idx_api_keys_hash ON api_keys(key_hash);
CREATE TRIGGER api_keys_updated_at BEFORE UPDATE ON api_keys
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- PHONE NUMBERS
-- source: 'manual' (hand-acquired) vs 'pool' (auto-provisioned, GC-eligible).
-- ============================================================
CREATE TABLE phone_numbers (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id        UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  e164                   TEXT NOT NULL UNIQUE,
  country_code           TEXT NOT NULL,
  number_type            TEXT NOT NULL CHECK (number_type IN ('local','mobile','toll_free')),
  capabilities           TEXT[] NOT NULL DEFAULT ARRAY['voice','sms'],
  source                 TEXT NOT NULL DEFAULT 'manual'
                         CHECK (source IN ('manual','pool')),
  provider               TEXT NOT NULL DEFAULT 'twilio',
  provider_resource_id   TEXT NOT NULL,
  provisioning_state     TEXT NOT NULL DEFAULT 'pending'
                         CHECK (provisioning_state IN ('pending','active','failed','released')),
  provisioning_metadata  JSONB NOT NULL DEFAULT '{}',
  acquired_at            TIMESTAMPTZ,
  released_at            TIMESTAMPTZ,
  last_used_at           TIMESTAMPTZ,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_phone_numbers_org ON phone_numbers(organization_id);
CREATE INDEX idx_phone_numbers_state ON phone_numbers(provisioning_state)
  WHERE provisioning_state IN ('pending','failed');
CREATE INDEX idx_phone_numbers_pool
  ON phone_numbers(organization_id, source, provisioning_state)
  WHERE source = 'pool';
CREATE TRIGGER phone_numbers_updated_at BEFORE UPDATE ON phone_numbers
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- CONVERSATIONS
-- Grouping of related comms for one task; external_id correlates
-- to the caller's own systems.
-- ============================================================
CREATE TABLE conversations (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
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
  organization_id       UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
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
CREATE INDEX idx_calls_from_number_active ON calls(from_number_id)
  WHERE status IN ('queued','dialing','ringing','in_progress');
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
  organization_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  request_hash     TEXT NOT NULL,
  response_status  INTEGER NOT NULL,
  response_body    JSONB NOT NULL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at       TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours'
);
CREATE INDEX idx_idempotency_expires ON idempotency_keys(expires_at);

-- ============================================================
-- AUDIT LOG — append-only record of mutating API actions.
-- ============================================================
CREATE TABLE audit_log (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  api_key_id      UUID REFERENCES api_keys(id) ON DELETE SET NULL,
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
  api_keys,
  organizations
CASCADE;
DROP FUNCTION IF EXISTS set_updated_at() CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
