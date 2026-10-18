"""Drop the unused conversations table and conversation_id columns.

0001 created ``conversations`` to group calls, emails and texts into one
thread, and ``calls.conversation_id`` / ``emails.conversation_id`` (0005)
to point at it. Nothing ever created a conversation: the API only copied
``conversation_id`` from the request, and with no rows the foreign key
rejected any value but NULL. On prod the table has 0 rows and both columns
are NULL on every row, so this drops no data.

Upgrade drops the two partial indexes, the two columns, the trigger and
the table (its indexes go with it). Downgrade recreates what 0001 and 0005
created; the columns come back NULL.

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_conversation")
    op.execute("DROP INDEX IF EXISTS idx_emails_conversation")
    op.execute("ALTER TABLE calls DROP COLUMN conversation_id")
    op.execute("ALTER TABLE emails DROP COLUMN conversation_id")
    op.execute("DROP TRIGGER IF EXISTS conversations_updated_at ON conversations")
    op.execute("DROP TABLE conversations")


DOWNGRADE = """
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

ALTER TABLE calls ADD COLUMN conversation_id UUID
  REFERENCES conversations(id) ON DELETE SET NULL;
CREATE INDEX idx_calls_conversation ON calls(conversation_id)
  WHERE conversation_id IS NOT NULL;

ALTER TABLE emails ADD COLUMN conversation_id UUID
  REFERENCES conversations(id) ON DELETE SET NULL;
CREATE INDEX idx_emails_conversation
  ON emails(conversation_id) WHERE conversation_id IS NOT NULL;
"""


def downgrade() -> None:
    op.execute(DOWNGRADE)
