"""Add carrier_route_failed to the call_end_reason ENUM.

Stamped by POST /calls when the from-number's carrier has no LiveKit
outbound trunk configured (e.g. LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID unset).
The call fails before any LiveKit room exists; maps to status='failed'.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction, hence the
autocommit block (same shape as 0039).

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS 'carrier_route_failed'"
        )


def downgrade() -> None:
    # Postgres cannot drop a single ENUM value; leaving it is harmless.
    pass
