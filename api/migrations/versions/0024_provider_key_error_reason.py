"""Add provider_key_error to the call_end_reason ENUM (BYO keys fail-fast)."""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'provider_key_error'"
        )


def downgrade() -> None:
    # Postgres cannot drop a single ENUM value; leaving it is harmless.
    pass
