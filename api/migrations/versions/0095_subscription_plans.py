"""Subscription plans: the plan of a workspace, its monthly allowance, the
plan's share of each call hold, and "paid from plan" ledger rows.

The billing site owns Stripe and writes ``organization_plans`` and
``plan_allowances``; hail reads them to let calls, texts and emails through
while the plan still covers them (hailhq.core.plans). hail stores amounts
only, never prices. Spec: docs/superpowers/specs/2026-11-25-subscription-plans-design.md.

Tables checked first:

* ``organizations``: owned by the billing site's auth migrations and kept
  lean; a subscription's state (status, period, Stripe ids) is job state.
* ``account_credits``: the wallet ledger. Plan minutes are not money, so a
  plan and its allowance are not rows here. What the plan paid for is: one
  ``kind = 'plan'`` row per use, with ``amount_cents = 0`` (the balance does
  not move) and ``plan_units`` (plan minutes, texts, emails or numbers), so
  the ledger shows every use and a replay is refused by the unique
  ``(organization_id, ref)`` index.
* ``organization_call_settings``: call behaviour the console edits; a plan is
  set by billing only, so it is kept where no console write path reaches.
* ``number_dunning``: one job's state (warn, then release). ``usage_events``:
  raw usage. ``platform_flags``: platform-wide. None holds a subscription or
  a monthly allowance, so ``organization_plans`` and ``plan_allowances`` are
  new.
* ``call_balance_holds``: what a live call sets aside. A call that the plan
  pays for also sets aside plan minutes, so the hold gets ``plan_minutes``.
* ``organization_knowledge``: the knowledge base limits of a workspace. A
  plan sets them too, so the row gets ``max_total_bytes`` (the workspace's
  cap on live knowledge data; NULL: none, as for existing rows) and
  ``source`` (``plan`` or ``staff``), so the billing site writes and deletes
  only the rows it owns.

``organization_plans``: one row per subscription. At most one open row
(``ended_at IS NULL``) per workspace. The plan is on only while ``status`` is
``active``. ``plan_allowances``: one row per workspace per plan month (yearly
plans too), with what the month includes and what it used.

No FK to ``organizations``: it lives in the billing site's schema, and hail
tables never reference it (see the contacts migration).

Revision ID: 0095
Revises: 0094
"""

from __future__ import annotations

from alembic import op

revision: str = "0095"
down_revision: str | None = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE organization_plans (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          organization_id UUID NOT NULL,
          tier TEXT NOT NULL
            CONSTRAINT organization_plans_tier_check
            CHECK (tier IN ('pro', 'business', 'enterprise')),
          step_hours INTEGER NOT NULL
            CONSTRAINT organization_plans_step_hours_check CHECK (step_hours > 0),
          billing_interval TEXT NOT NULL
            CONSTRAINT organization_plans_interval_check
            CHECK (billing_interval IN ('month', 'year')),
          status TEXT NOT NULL
            CONSTRAINT organization_plans_status_check
            CHECK (status IN ('incomplete', 'incomplete_expired', 'trialing',
                              'active', 'past_due', 'canceled', 'unpaid',
                              'paused')),
          stripe_customer_id TEXT NOT NULL,
          stripe_subscription_id TEXT NOT NULL
            CONSTRAINT organization_plans_subscription_uq UNIQUE,
          started_at TIMESTAMPTZ NOT NULL,
          current_period_end TIMESTAMPTZ NOT NULL,
          cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
          ended_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          -- A subscription in a final Stripe status has ended.
          CONSTRAINT organization_plans_ended_check
            CHECK (ended_at IS NOT NULL
                   OR status NOT IN ('canceled', 'incomplete_expired'))
        )
        """)
    op.execute("""
        CREATE UNIQUE INDEX organization_plans_open_uq
          ON organization_plans (organization_id)
          WHERE ended_at IS NULL
        """)
    op.execute("""
        CREATE TABLE plan_allowances (
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
          organization_id UUID NOT NULL,
          plan_id UUID NOT NULL
            CONSTRAINT plan_allowances_plan_id_fkey
            REFERENCES organization_plans (id) ON DELETE CASCADE,
          period_start TIMESTAMPTZ NOT NULL,
          period_end TIMESTAMPTZ NOT NULL,
          included_minutes NUMERIC(14,3) NOT NULL,
          used_minutes NUMERIC(14,3) NOT NULL DEFAULT 0,
          included_texts INTEGER NOT NULL,
          used_texts INTEGER NOT NULL DEFAULT 0,
          included_emails INTEGER NOT NULL,
          used_emails INTEGER NOT NULL DEFAULT 0,
          included_numbers INTEGER NOT NULL,
          used_numbers INTEGER NOT NULL DEFAULT 0,
          -- Destination countries (ISO 3166-1 alpha-2) whose texts use
          -- included texts. Texts elsewhere are paid from credits.
          text_countries TEXT[] NOT NULL DEFAULT '{}',
          -- The most the plan pays toward one included number's monthly fee;
          -- a dearer number pays the rest from credits.
          number_fee_cap_cents NUMERIC(14,1) NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT plan_allowances_period_check CHECK (period_end > period_start),
          CONSTRAINT plan_allowances_amounts_check CHECK (
            included_minutes >= 0 AND used_minutes >= 0
            AND included_texts >= 0 AND used_texts >= 0
            AND included_emails >= 0 AND used_emails >= 0
            AND included_numbers >= 0 AND used_numbers >= 0
            AND number_fee_cap_cents >= 0),
          CONSTRAINT plan_allowances_org_period_uq
            UNIQUE (organization_id, period_start)
        )
        """)
    op.execute("""
        CREATE INDEX plan_allowances_org_period_idx
          ON plan_allowances (organization_id, period_end)
        """)
    op.execute("""
        CREATE INDEX plan_allowances_plan_idx ON plan_allowances (plan_id)
        """)

    # "Paid from plan" ledger rows: kind 'plan', 0 cents, plan_units used
    # (negative to give units back, as a settlement correction does).
    op.execute("ALTER TABLE account_credits ADD COLUMN plan_units NUMERIC(14,3)")
    op.drop_constraint("account_credits_kind_check", "account_credits", type_="check")
    op.create_check_constraint(
        "account_credits_kind_check",
        "account_credits",
        "kind IN ('credit', 'debit', 'plan')",
    )
    op.drop_constraint(
        "account_credits_amount_sign_check", "account_credits", type_="check"
    )
    op.create_check_constraint(
        "account_credits_amount_sign_check",
        "account_credits",
        "(kind = 'credit' AND amount_cents > 0) OR "
        "(kind = 'debit' AND amount_cents < 0) OR "
        "(kind = 'plan' AND amount_cents = 0)",
    )
    op.create_check_constraint(
        "account_credits_plan_units_check",
        "account_credits",
        "(kind = 'plan') = (plan_units IS NOT NULL) AND "
        "(plan_units IS NULL OR plan_units <> 0)",
    )

    op.execute("""
        ALTER TABLE call_balance_holds
          ADD COLUMN plan_minutes NUMERIC(14,3) NOT NULL DEFAULT 0
          CONSTRAINT call_balance_holds_plan_minutes_check
          CHECK (plan_minutes >= 0)
        """)
    op.execute("""
        ALTER TABLE organization_knowledge
          ADD COLUMN max_total_bytes BIGINT
            CONSTRAINT organization_knowledge_total_bytes_check
            CHECK (max_total_bytes IS NULL OR max_total_bytes > 0),
          ADD COLUMN source TEXT NOT NULL DEFAULT 'staff'
            CONSTRAINT organization_knowledge_source_check
            CHECK (source IN ('plan', 'staff'))
        """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE organization_knowledge"
        " DROP COLUMN source, DROP COLUMN max_total_bytes"
    )
    op.execute("ALTER TABLE call_balance_holds DROP COLUMN plan_minutes")
    op.execute("DELETE FROM account_credits WHERE kind = 'plan'")
    op.drop_constraint(
        "account_credits_plan_units_check", "account_credits", type_="check"
    )
    op.drop_constraint(
        "account_credits_amount_sign_check", "account_credits", type_="check"
    )
    op.create_check_constraint(
        "account_credits_amount_sign_check",
        "account_credits",
        "(kind = 'credit' AND amount_cents > 0) OR "
        "(kind = 'debit' AND amount_cents < 0)",
    )
    op.drop_constraint("account_credits_kind_check", "account_credits", type_="check")
    op.create_check_constraint(
        "account_credits_kind_check",
        "account_credits",
        "kind IN ('credit','debit')",
    )
    op.execute("ALTER TABLE account_credits DROP COLUMN plan_units")
    op.drop_table("plan_allowances")
    op.drop_table("organization_plans")
