"""outbound email: sender_domains + emails

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-16

Two new tables:

* ``sender_domains`` — registered sending identities. Two flavors:
  ``kind='hail_mail'`` (per-org local-part under the operator's
  pre-verified parent domain, lands ``verified``) and ``kind='custom'``
  (tenant DNS, lands ``pending`` until they publish the DKIM CNAMEs we
  hand back from SES).
* ``emails`` — one row per outbound message, mirrors the calls table
  shape (queued → sent / failed / bounced / complained).

``account_credits.channel`` and ``usage_events.channel`` already accept
``'email'`` (set in migration 0001 anticipating the v1.4 milestone), so
no new CHECK changes are needed there.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


UPGRADE = """
-- ============================================================
-- SENDER DOMAINS — sending identities registered with the email provider.
-- ============================================================
CREATE TABLE sender_domains (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      UUID NOT NULL,
  kind                 TEXT NOT NULL CHECK (kind IN ('hail_mail','custom')),
  domain               TEXT NOT NULL,
  -- Local-part prefixes used to compose hail-mail addresses
  -- ``<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>``. Both NULL for custom rows,
  -- both required for hail-mail rows (enforced below).
  local_prefix_user    TEXT,
  local_prefix_org     TEXT,
  verification_status  TEXT NOT NULL DEFAULT 'pending'
                       CHECK (verification_status IN ('pending','verified','failed')),
  -- JSONB array of {name, value, type} CNAMEs the tenant must publish.
  dkim_records         JSONB NOT NULL DEFAULT '[]'::jsonb,
  mail_from_domain     TEXT,
  provider             TEXT NOT NULL DEFAULT 'ses',
  -- Provider-native identity id (SES uses the domain itself).
  provider_resource_id TEXT,
  verified_at          TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT sender_domains_org_domain_unique UNIQUE (organization_id, domain),
  CONSTRAINT sender_domains_prefix_kind_consistency CHECK (
    (kind = 'hail_mail' AND local_prefix_user IS NOT NULL AND local_prefix_org IS NOT NULL)
    OR (kind = 'custom' AND local_prefix_user IS NULL AND local_prefix_org IS NULL)
  )
);
CREATE INDEX idx_sender_domains_org
  ON sender_domains(organization_id);
-- Drives "do I already have a verified domain to send from?" on POST /emails.
CREATE INDEX idx_sender_domains_org_verified
  ON sender_domains(organization_id, created_at ASC)
  WHERE verification_status = 'verified';
CREATE TRIGGER sender_domains_updated_at BEFORE UPDATE ON sender_domains
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ============================================================
-- EMAILS — one row per outbound message.
-- ============================================================
CREATE TABLE emails (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      UUID NOT NULL,
  conversation_id      UUID REFERENCES conversations(id) ON DELETE SET NULL,
  sender_domain_id     UUID NOT NULL REFERENCES sender_domains(id) ON DELETE RESTRICT,
  from_address         TEXT NOT NULL,
  to_addresses         TEXT[] NOT NULL CHECK (array_length(to_addresses, 1) >= 1),
  cc_addresses         TEXT[],
  bcc_addresses        TEXT[],
  reply_to             TEXT,
  subject              TEXT NOT NULL,
  body_text            TEXT,
  body_html            TEXT,
  status               TEXT NOT NULL DEFAULT 'queued'
                       CHECK (status IN ('queued','sent','failed','bounced','complained')),
  end_reason           TEXT,
  provider             TEXT NOT NULL DEFAULT 'ses',
  provider_message_id  TEXT UNIQUE,
  requested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  sent_at              TIMESTAMPTZ,
  failed_at            TIMESTAMPTZ,
  metadata             JSONB NOT NULL DEFAULT '{}',
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The application enforces "body_text OR body_html"; mirror it at the
  -- schema level so a misbehaving writer can't slip an empty row in.
  CHECK (body_text IS NOT NULL OR body_html IS NOT NULL)
);
CREATE INDEX idx_emails_org_created
  ON emails(organization_id, created_at DESC);
CREATE INDEX idx_emails_sender_domain
  ON emails(sender_domain_id);
CREATE INDEX idx_emails_conversation
  ON emails(conversation_id) WHERE conversation_id IS NOT NULL;
"""


DOWNGRADE = """
DROP INDEX IF EXISTS idx_emails_conversation;
DROP INDEX IF EXISTS idx_emails_sender_domain;
DROP INDEX IF EXISTS idx_emails_org_created;
DROP TABLE IF EXISTS emails;

DROP TRIGGER IF EXISTS sender_domains_updated_at ON sender_domains;
DROP INDEX IF EXISTS idx_sender_domains_org_verified;
DROP INDEX IF EXISTS idx_sender_domains_org;
DROP TABLE IF EXISTS sender_domains;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
