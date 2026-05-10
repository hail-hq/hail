"""switch api-key auth to consume the auth backend's apikey table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-09

hail/api stops minting/storing keys. The auth backend (in hail-website) is the
sole producer; the `apikey` table it owns lives in the same Postgres. This
migration:

* Adds organizations.auth_user_id (TEXT, unique) — the bridge column. Lazy-
  populated by hail/api on the first authenticated request from a new user.
* Drops api_keys table and the audit_log → api_keys FK. audit_log.api_key_id
  becomes a free-form TEXT column carrying the auth backend's apikey.id.

Downgrade recreates the table structure but does NOT restore the dropped key
rows — running it after upgrade is effectively a destructive reset.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPGRADE = """
ALTER TABLE organizations
  ADD COLUMN auth_user_id TEXT;
CREATE UNIQUE INDEX idx_organizations_auth_user_id
  ON organizations(auth_user_id)
  WHERE auth_user_id IS NOT NULL;

ALTER TABLE audit_log
  DROP CONSTRAINT IF EXISTS audit_log_api_key_id_fkey;
ALTER TABLE audit_log
  ALTER COLUMN api_key_id TYPE TEXT USING api_key_id::text;

DROP TABLE IF EXISTS api_keys CASCADE;
"""


DOWNGRADE = """
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

ALTER TABLE audit_log
  ALTER COLUMN api_key_id TYPE UUID USING api_key_id::uuid;
ALTER TABLE audit_log
  ADD CONSTRAINT audit_log_api_key_id_fkey
    FOREIGN KEY (api_key_id) REFERENCES api_keys(id) ON DELETE SET NULL;

DROP INDEX IF EXISTS idx_organizations_auth_user_id;
ALTER TABLE organizations DROP COLUMN auth_user_id;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
