"""email_accounts: tenant-connected Gmail mailboxes + emails linkage."""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, UUID

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_accounts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), server_default="gmail", nullable=False),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("provider_user_id", sa.Text(), nullable=False),
        sa.Column("scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "provider IN ('gmail')", name="email_accounts_provider_check"
        ),
        sa.CheckConstraint(
            "status IN ('active','reauth_required','disabled')",
            name="email_accounts_status_check",
        ),
    )
    op.create_index(
        "email_accounts_address_global_uq",
        "email_accounts",
        ["email_address"],
        unique=True,
    )
    op.create_index("email_accounts_org_idx", "email_accounts", ["organization_id"])

    op.add_column(
        "emails", sa.Column("email_account_id", UUID(as_uuid=True), nullable=True)
    )
    op.add_column("emails", sa.Column("provider_thread_id", sa.Text(), nullable=True))
    op.create_foreign_key(
        "emails_email_account_id_fkey",
        "emails",
        "email_accounts",
        ["email_account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("emails_email_account_id_idx", "emails", ["email_account_id"])
    op.drop_constraint("emails_outbound_has_domain", "emails", type_="check")
    op.create_check_constraint(
        "emails_outbound_has_sender",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL "
        "OR email_account_id IS NOT NULL",
    )
    op.create_check_constraint(
        "emails_one_sender_kind",
        "emails",
        "email_domain_id IS NULL OR email_account_id IS NULL",
    )


def downgrade() -> None:
    op.drop_constraint("emails_one_sender_kind", "emails", type_="check")
    op.drop_constraint("emails_outbound_has_sender", "emails", type_="check")
    # Connected-account sends are outbound rows with email_domain_id NULL —
    # they violate the pre-feature emails_outbound_has_domain check and only
    # exist because of this feature, so drop them before restoring the old
    # constraint (otherwise this downgrade is irreversible once any Gmail
    # send has been recorded).
    op.execute("DELETE FROM emails WHERE email_account_id IS NOT NULL")
    op.create_check_constraint(
        "emails_outbound_has_domain",
        "emails",
        "direction = 'inbound' OR email_domain_id IS NOT NULL",
    )
    op.drop_index("emails_email_account_id_idx", table_name="emails")
    op.drop_constraint("emails_email_account_id_fkey", "emails", type_="foreignkey")
    op.drop_column("emails", "provider_thread_id")
    op.drop_column("emails", "email_account_id")
    op.drop_index("email_accounts_org_idx", table_name="email_accounts")
    op.drop_index("email_accounts_address_global_uq", table_name="email_accounts")
    op.drop_table("email_accounts")
