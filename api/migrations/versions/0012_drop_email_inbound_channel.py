"""Collapse the ``email_inbound`` channel back into ``email``.

Revision 0011 widened the ``usage_events`` and ``account_credits`` channel
CHECKs to allow a separate ``email_inbound`` value. Billing has since
collapsed inbound email into the single ``email`` channel, so the value must
be gone from the schema — not merely unused. Databases that ran 0011 (and may
hold ``email_inbound`` rows from when it was live) reach the clean state by
running this forward migration: convert any such rows to ``email`` first, then
narrow both CHECKs to forbid the value.

The conversion must precede the narrow — Postgres validates the new CHECK
against existing rows, so a leftover ``email_inbound`` row would otherwise
abort the constraint swap.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Convert any rows written while the email_inbound channel was live. Both
    # tables, so neither the rater nor the ledger is left with an orphan value.
    op.execute(
        "UPDATE usage_events SET channel = 'email' WHERE channel = 'email_inbound'"
    )
    op.execute(
        "UPDATE account_credits SET channel = 'email' WHERE channel = 'email_inbound'"
    )

    op.drop_constraint("usage_events_channel_check", "usage_events", type_="check")
    op.create_check_constraint(
        "usage_events_channel_check",
        "usage_events",
        "channel IN ('voice','sms','email')",
    )
    op.drop_constraint(
        "account_credits_channel_check", "account_credits", type_="check"
    )
    op.create_check_constraint(
        "account_credits_channel_check",
        "account_credits",
        "channel IN ('voice','sms','email','credit')",
    )


def downgrade() -> None:
    # Re-widen to 0011's state. Rows are not converted back — ``email`` is the
    # collapsed value and cannot be told apart from genuine outbound email.
    op.drop_constraint("usage_events_channel_check", "usage_events", type_="check")
    op.create_check_constraint(
        "usage_events_channel_check",
        "usage_events",
        "channel IN ('voice','sms','email','email_inbound')",
    )
    op.drop_constraint(
        "account_credits_channel_check", "account_credits", type_="check"
    )
    op.create_check_constraint(
        "account_credits_channel_check",
        "account_credits",
        "channel IN ('voice','sms','email','email_inbound','credit')",
    )
