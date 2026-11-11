"""Browser conversations extend calls rather than creating a sibling table.

Checked calls (session lifecycle, transcript, agent), call_events (session
observations), usage_events (metering) and account_credits (money). Calls is
the existing voice session owner. Reservations live in its metadata and
unused paid reservations are returned through the existing ledger.
"""

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "calls",
        sa.Column("transport", sa.Text(), server_default="phone", nullable=False),
    )
    op.alter_column("calls", "from_e164", nullable=True)
    op.alter_column("calls", "to_e164", nullable=True)
    op.drop_constraint("calls_number_for_direction", "calls", type_="check")
    op.create_check_constraint(
        "calls_number_for_direction",
        "calls",
        "(transport = 'browser' AND from_number_id IS NULL AND to_number_id IS NULL AND from_e164 IS NULL AND to_e164 IS NULL) OR (transport = 'phone' AND from_e164 IS NOT NULL AND to_e164 IS NOT NULL AND ((direction = 'outbound' AND from_number_id IS NOT NULL) OR (direction = 'inbound' AND to_number_id IS NOT NULL)))",
    )
    op.create_check_constraint(
        "calls_transport_check", "calls", "transport IN ('phone','browser')"
    )


def downgrade():
    # Browser history must be exported or removed explicitly before rollback.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM calls WHERE transport='browser') THEN RAISE EXCEPTION 'browser calls must be handled before downgrade'; END IF; END $$"
    )
    op.drop_constraint("calls_number_for_direction", "calls", type_="check")
    op.drop_constraint("calls_transport_check", "calls", type_="check")
    op.alter_column("calls", "from_e164", nullable=False)
    op.alter_column("calls", "to_e164", nullable=False)
    op.create_check_constraint(
        "calls_number_for_direction",
        "calls",
        "(direction='outbound' AND from_number_id IS NOT NULL) OR (direction='inbound' AND to_number_id IS NOT NULL)",
    )
    op.drop_column("calls", "transport")
