"""Add llm_endpoint_failed to the call_end_reason ENUM (mode B BYO give-up).

Stamped by the voicebot when a per-call BYO llm endpoint returns
non-recoverable errors on 3 consecutive turns; maps to status='failed'.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction, hence the
autocommit block (same shape as 0024).

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'llm_endpoint_failed'"
        )


def downgrade() -> None:
    # Postgres cannot drop a single ENUM value; leaving it is harmless.
    pass
