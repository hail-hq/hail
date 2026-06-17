"""Allow the ``email_inbound`` channel on usage_events and account_credits.

Inbound email billing meters each received message as a distinct channel
(``email_inbound``) so the rater can price and label it separately from
outbound sends. The original ``usage_events_channel_check`` only permitted
``voice``/``sms``/``email``; ``account_credits_channel_check`` permitted
``voice``/``sms``/``email``/``credit``. Postgres can't widen a CHECK in
place, so this drops and re-adds both with the new value.

No existing rows can violate the wider constraints (they are supersets), so
the re-adds are safe without pre-flight checks.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_constraint(
        "account_credits_channel_check", "account_credits", type_="check"
    )
    op.create_check_constraint(
        "account_credits_channel_check",
        "account_credits",
        "channel IN ('voice','sms','email','credit')",
    )
    op.drop_constraint("usage_events_channel_check", "usage_events", type_="check")
    op.create_check_constraint(
        "usage_events_channel_check",
        "usage_events",
        "channel IN ('voice','sms','email')",
    )
