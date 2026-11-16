"""Add sms_trial_phones: the one phone a workspace proves for US trial texts.

Tables checked, and why each does not fit:
- email_forward_targets: closest record, but its unique index on token_hash is
  global, and 6-digit codes collide across workspaces. Its uniqueness is many
  rows per workspace; the trial needs one row per workspace and one workspace
  per verified phone. Every forward query reads status='verified' with no
  channel filter, so a phone row could become an email forward address. It has
  no try counter or code-send window.
- contacts, agent_handover_contacts: directory people, no proof state.
- suppressions: opposite meaning (blocked, not proven).
- carrier_verifications: carrier filings, not phone proof.
- website users.sms_opt_in_*: per user, account notices, no proof.
organization_id has no FK (the website owns that table).
Also adds a partial index on sms to count a workspace's trial texts.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sms_trial_phones",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("pending_phone_e164", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("code_hash", sa.Text(), nullable=True),
        sa.Column("code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "code_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "code_send_log",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("consent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consent_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("organization_id", name="sms_trial_phones_org_uq"),
        sa.CheckConstraint(
            "status IN ('pending','verified')",
            name="sms_trial_phones_status_check",
        ),
        sa.CheckConstraint(
            r"phone_e164 ~ '^\+[1-9][0-9]{6,14}$'",
            name="sms_trial_phones_phone_e164_check",
        ),
        sa.CheckConstraint(
            r"pending_phone_e164 ~ '^\+[1-9][0-9]{6,14}$'",
            name="sms_trial_phones_pending_phone_e164_check",
        ),
    )
    op.create_index(
        "sms_trial_phones_verified_phone_uq",
        "sms_trial_phones",
        ["phone_e164"],
        unique=True,
        postgresql_where=sa.text("status = 'verified'"),
    )
    op.create_index(
        "sms_trial_org_idx",
        "sms",
        ["organization_id"],
        postgresql_where=sa.text("metadata ? 'sms_trial'"),
    )


def downgrade():
    op.drop_index("sms_trial_org_idx", table_name="sms")
    op.drop_index("sms_trial_phones_verified_phone_uq", table_name="sms_trial_phones")
    op.drop_table("sms_trial_phones")
