"""Add ``call_balance_holds``: money a live call sets aside.

Hail charges a call after it ends, so the balance does not drop while the
call runs. Each billed call now sets aside what it may cost (its price per
minute times the minutes it may run), and the next call sees the balance
less every live hold (hailhq.core.call_allowance).

Tables checked first:
- ``calls``: holds would add billing job state to an entity table, and
  keeping them in ``calls.metadata`` as text needs format checks on every
  read.
- ``account_credits``: the ledger. A hold is not money spent; writing it
  there would show in the balance and need a refund row per call.
- ``number_dunning``, ``carrier_verifications``: other records (low-balance
  notices, carrier filings), not money set aside for a call.
- ``call_events``: one row per handover result, written by the voicebot
  after the dial ends. The hold must count a dial when the API approves it,
  under the organization's lock, before any result exists.

One row per call; the organization is the call's. ``dials`` counts the
handover dials approved for the call (each may bill the second line). The
prices are the highest held for the call: once it ends, the hold counts its
real minutes at them (NULL: a price Hail does not know, so the full hold
counts). The amount and the end time only grow; a price only grows, or
becomes NULL for good. Rows a day past their end are deleted, 100 at a
time, when a hold is written.

Revision ID: 0079
Revises: 0078
"""

from __future__ import annotations

from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE call_balance_holds (
          call_id    UUID PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
          cents      NUMERIC(14,1) NOT NULL CHECK (cents >= 0),
          hold_until TIMESTAMPTZ NOT NULL,
          cents_per_minute NUMERIC,
          cents_per_call   NUMERIC,
          dials      INTEGER NOT NULL DEFAULT 0 CHECK (dials >= 0),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT call_balance_holds_prices_check
            CHECK (cents_per_minute >= 0 AND cents_per_call >= 0)
        )
        """)
    op.execute("""
        CREATE INDEX call_balance_holds_until_idx
          ON call_balance_holds (hold_until)
        """)


def downgrade() -> None:
    op.execute("DROP TABLE call_balance_holds")
