"""Carrier legs of a call, with what the carrier charged for each.

One row per phone line a call used: the caller's leg (or the person Hail
dialed) and one per handover dial. The voicebot writes the rows when the
call ends. A background pass in the api reads each carrier's call record
and fills in ``cost_usd``, ``billed_seconds`` and ``usd_per_minute``.
Billing reads these costs later; nothing reads them yet.

Tables checked: ``calls`` holds one row per call, and a call has several
legs. ``call_events`` is returned by the public events API, and carrier
costs must stay internal. ``usage_events`` holds unit counts for the rater,
with no money and no carrier. None fits, so this adds a table.

Downgrade drops the table and its costs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "call_legs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "call_id",
            UUID(as_uuid=True),
            sa.ForeignKey("calls.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("sip_call_id", sa.Text(), nullable=True),
        sa.Column("carrier_call_id", sa.Text(), nullable=True),
        sa.Column("to_e164", sa.Text(), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cost_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("billed_seconds", sa.Integer(), nullable=True),
        sa.Column("usd_per_minute", sa.Numeric(14, 6), nullable=True),
        sa.Column("cost_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('call','handover')", name="call_legs_role_check"),
        sa.CheckConstraint(
            "direction IN ('inbound','outbound')", name="call_legs_direction_check"
        ),
        sa.CheckConstraint(
            "provider IN ('twilio','telnyx','didww')", name="call_legs_provider_check"
        ),
        sa.CheckConstraint(
            "cost_status IN ('pending','found','missing')",
            name="call_legs_cost_status_check",
        ),
        sa.CheckConstraint(
            "(cost_status = 'found') = (cost_usd IS NOT NULL)",
            name="call_legs_cost_when_found",
        ),
    )
    op.create_index(
        "call_legs_call_sip_uq",
        "call_legs",
        ["call_id", "sip_call_id"],
        unique=True,
        postgresql_where=sa.text("sip_call_id IS NOT NULL"),
    )
    op.create_index(
        "call_legs_pending_idx",
        "call_legs",
        ["joined_at"],
        postgresql_where=sa.text("cost_status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("call_legs")
