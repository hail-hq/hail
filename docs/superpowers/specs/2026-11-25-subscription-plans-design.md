# Subscription plans: hail side

A workspace can hold a monthly or yearly plan (Pro, Business or Enterprise).
Each plan month includes call minutes, texts, emails and phone numbers. Hail
lets calls, texts and emails through while the plan still covers them, and
pays the first month of an included number from the plan. Use beyond the plan
is paid from credits, as today.

The billing site owns Stripe and writes the plan rows. hail reads them. hail
stores amounts only, never prices.

## Who writes what

| Table / column                                                               | Written by                                              | Read by                  |
| ---------------------------------------------------------------------------- | ------------------------------------------------------- | ------------------------ |
| `organization_plans`                                                         | billing site (Stripe sync)                              | hail gates, billing site |
| `plan_allowances` included amounts, `text_countries`, `number_fee_cap_cents` | billing site, at each period start and on a plan change | hail gates               |
| `plan_allowances.used_minutes`, `used_texts`, `used_emails`                  | billing site raters                                     | hail gates               |
| `account_credits` rows with `kind = 'plan'`                                  | billing site raters; hail for a number's first month    | console, both            |
| `call_balance_holds.plan_minutes`                                            | hail (call start, handover)                             | hail call cap            |
| `organization_knowledge` (`source = 'plan'` rows)                            | billing site, from the plan                             | hail knowledge base      |
| `organization_knowledge` (`source = 'staff'` rows)                           | Hail staff                                              | hail knowledge base      |

## Schema

### `organization_plans`: one row per subscription

| Column                     | Type                        | Rule                                                                                                           |
| -------------------------- | --------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `id`                       | uuid PK                     | `gen_random_uuid()`                                                                                            |
| `organization_id`          | uuid NOT NULL               | no FK (`organizations` is the billing site's table)                                                            |
| `tier`                     | text NOT NULL               | `pro`, `business`, `enterprise`                                                                                |
| `step_hours`               | int NOT NULL                | > 0 (Standard hours a month)                                                                                   |
| `billing_interval`         | text NOT NULL               | `month`, `year`                                                                                                |
| `status`                   | text NOT NULL               | Stripe's: `incomplete`, `incomplete_expired`, `trialing`, `active`, `past_due`, `canceled`, `unpaid`, `paused` |
| `stripe_customer_id`       | text NOT NULL               |                                                                                                                |
| `stripe_subscription_id`   | text NOT NULL UNIQUE        |                                                                                                                |
| `started_at`               | timestamptz NOT NULL        |                                                                                                                |
| `current_period_end`       | timestamptz NOT NULL        | from `items.data[].current_period_end`                                                                         |
| `cancel_at_period_end`     | bool NOT NULL default false |                                                                                                                |
| `ended_at`                 | timestamptz NULL            | set when the subscription is deleted                                                                           |
| `created_at`, `updated_at` | timestamptz                 |                                                                                                                |

- Partial unique index `organization_plans_open_uq` on `(organization_id) WHERE ended_at IS NULL`: one open row per workspace.
- CHECK: a row in `canceled` or `incomplete_expired` has `ended_at` set.
- The plan is **on** only when `ended_at IS NULL AND status = 'active'`. `past_due`, `unpaid` and every other status turn it off at once.
- A plan change (upgrade, downgrade taking effect) updates the row in place. A new subscription after a cancel is a new row.
- The calls-at-the-same-time cap is not in this change. Its PR adds `max_concurrent_calls`.

### `plan_allowances`: one row per workspace per plan month

| Column                           | Type                             | Rule                                                                                                           |
| -------------------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `id`                             | uuid PK                          |                                                                                                                |
| `organization_id`                | uuid NOT NULL                    |                                                                                                                |
| `plan_id`                        | uuid NOT NULL                    | FK `organization_plans(id) ON DELETE CASCADE`                                                                  |
| `period_start`, `period_end`     | timestamptz NOT NULL             | `period_end > period_start`; UNIQUE `(organization_id, period_start)`                                          |
| `included_minutes`               | numeric(14,3) NOT NULL           | plan minutes (Standard minute = 1, Plus minute = 2)                                                            |
| `used_minutes`                   | numeric(14,3) NOT NULL default 0 |                                                                                                                |
| `included_texts`, `used_texts`   | int NOT NULL                     | text segments                                                                                                  |
| `included_emails`, `used_emails` | int NOT NULL                     | messages (sent, received or forwarded)                                                                         |
| `included_numbers`               | int NOT NULL                     | how many numbers the plan pays the monthly fee of: the workspace's oldest numbers with a fee (no used counter) |
| `text_countries`                 | text[] NOT NULL default `{}`     | destination countries (ISO alpha-2) whose texts use included texts                                             |
| `number_fee_cap_cents`           | numeric(14,1) NOT NULL default 0 | the most the plan pays toward one included number's monthly fee                                                |
| `created_at`, `updated_at`       | timestamptz                      |                                                                                                                |

- All amounts are >= 0 (CHECK).
- Yearly plans get a row for each month too.
- Upgrade mid-month: the billing site raises the current row's included amounts. Used amounts stay.
- Periods of one workspace must not overlap. hail reads the row with `period_start <= now < period_end` whose plan is on.
- `text_countries` holds only countries whose text costs are known and whose price keeps the markup rule. Today that is `{US}`.

### "Paid from plan" ledger rows (`account_credits`)

The ledger gets a third kind next to `credit` and `debit`:

- `kind = 'plan'`, `amount_cents = 0` (the balance does not move).
- New column `plan_units numeric(14,3)`: plan minutes, texts, emails or numbers the plan paid. Negative gives units back (a settlement correction). NOT NULL and non-zero exactly when `kind = 'plan'` (CHECK `account_credits_plan_units_check`).
- `channel`: `voice`, `sms` or `email`, as for the debit the row stands in for. A number uses `voice` when it can make calls, else `sms` (same rule as its fee).
- `source`: `plan` for usage, `plan.monthly_fee` for a number's monthly fee.
- `ref`: unique per workspace, like every row. One usage event can be part plan, part credits, so each part has its own ref:
  - usage: `plan:<the ref the credits debit of that event uses>`; settlement correction: `plan:<the settlement ref>`.
  - number fee: the plan row takes the month's normal fee ref (`monthly_fee:<org>:<number>:dedicated_number:<YYYY-MM>`), so the renewal rater's "this month already has a row" check stays true. The part above `number_fee_cap_cents` is a debit with that ref plus `:over_plan`.
- Balance stays `SUM(amount_cents)`. Code that branches on `kind` must handle `plan`.

**Rater rule:** in one transaction, under the workspace lock, the rater inserts the plan row (`ON CONFLICT (organization_id, ref) DO NOTHING`), raises the matching `used_*` counter only when the row was inserted, writes the credits debit for the rest, and stamps `usage_events.priced_at`. The call cap reads `used_minutes` and the holds in one statement, so a rater commit is counted once, never twice and never zero times.

### `call_balance_holds.plan_minutes`

- `numeric(14,3) NOT NULL DEFAULT 0`, CHECK >= 0.
- The plan minutes a live call sets aside. A hold only grows. It counts until the call's usage is priced, under the same rules as `cents` (see `hailhq.core.call_allowance`), except that an ended, unpriced call counts its full plan minutes.

## Call price quote: two new optional fields

`POST /api/internal/voice-rates/quote` (billing site) adds two fields to its reply. Request unchanged. Reply example for a Standard leg with recording on:

```json
{
  "cents_per_minute": 11,
  "cents_per_call": 0,
  "plan_minutes_per_minute": 1,
  "plan_cents_per_minute": 10
}
```

Two lines (handover), one Standard and one Plus: `plan_minutes_per_minute: 3`, `plan_cents_per_minute: 30`. An Other leg adds 0 to both.

| Field                     | Meaning                                                                                                                     | Absent |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------- | ------ |
| `plan_minutes_per_minute` | plan minutes one call minute uses: the sum over the legs of each leg's group weight (Standard 1, Plus 2, Other 0)           | 0      |
| `plan_cents_per_minute`   | the part of `cents_per_minute` a plan minute pays (the legs' Standard and Plus group prices; not recording, not Other legs) | 0      |

- `0 <= plan_cents_per_minute <= cents_per_minute`, and both fields are 0 or both above 0. Otherwise hail ignores both and logs a warning (`hailhq.core.internal_webhook.CallQuote`).
- The route returns them for every workspace. hail decides whether a plan month exists.
- With both fields absent, or no plan, the cap is exactly today's. hail can deploy first.

## Funds gate

`has_funds(db, org, use=None, *, country=None)` in `hailhq.core.billing`:

- True when the balance is above 0, as today.
- Also true when `use` is given and the plan is on with an allowance for now that still covers it:
  - `call`: `used_minutes < included_minutes`.
  - `text`: `country` is in `text_countries` and `used_texts < included_texts`.
  - `email`: `used_emails < included_emails`.
- `use=None` (number purchases, verifications): balance only.
- Every call site passes its use. The 2-argument `FundsCheck` callbacks (email forwarding) get `has_email_funds`.
- The raters run on a schedule, so the gate sees use a few minutes late. A small overrun is paid from credits.

## Call balance cap

A call may run for what the plan and the balance pay together.

1. `plan_left = included_minutes - used_minutes - held plan minutes of other calls` (one statement, `plan_minutes_left`).
2. `P = floor(plan_left / plan_minutes_per_minute)` call minutes from the plan.
3. During plan minutes the balance pays `cents_per_minute - plan_cents_per_minute` a minute (recording, Other legs) plus `cents_per_call`. After them it pays the full price.
4. The hold stores the balance part in `cents` and `plan_minutes_per_minute x plan minutes` in `plan_minutes`.

A handover prices the time already talked and the ring with the one-line quote (plan first), then the handover with the two-line quote on the plan minutes left.

## Numbers

One rule, used by hail for a purchase and by the billing site's renewal rater each month: while the plan is on, it pays the monthly fee of the workspace's `included_numbers` **oldest** numbers that have a fee (not pool numbers, not connected phone system numbers; active or being ordered), up to `number_fee_cap_cents` each. A dearer number pays the rest from credits. Every other number is paid from credits, as today. There is no used counter: the rank decides (`hailhq.core.plans.plan_number_cents`). Rank by `(created_at, id)`, ascending. A number the plan already paid this calendar month (a `plan` row on its fee ref) keeps its place until the month ends, even once released, and counts against every other number whatever its rank, so a release and a new purchase never get a second included month. The renewal rater must use exactly this rule. A plan that starts mid-month pays nothing for numbers whose fee for that month was already paid from credits; it pays them from the next calendar month.

- A calendar month's fee is decided when it is charged, from the plan month row that covers that moment. Fee refs stay UTC calendar months.
- Releasing a number moves the next oldest into the included ones from its next fee.

Number bought in the API, when the rule says the plan pays:

- the order reserves only the credits part (setup plus the rest of the first month), and its 402 names that amount;
- when the order finishes, the rule is checked again (the plan may have stopped, or the order finished late). Plan pays: a plan row (`kind = 'plan'`, `plan_units = 1`, ref = the month's fee ref, source `plan.monthly_fee`), plus a debit for the part above the cap with ref + `:over_plan`. Otherwise the full fee is debited, as today.

The renewal rater writes the same rows each month for the numbers the rule covers. It skips a month whose fee ref already has a row, as today.

## Feature gates (`hailhq.core.plans`)

- `plan_allows(db, org, feature)`: one table maps each feature to the tiers that include it:
  - `porting`: Pro, Business, Enterprise.
  - `connected_phone_systems`: Business, Enterprise.
- A new feature is one new entry in the table.
- `active_plan(db, org)` and `current_allowance(db, org)` return the rows the gates read.
- The porting and connected-phone-system PRs call `plan_allows` in place of their staff checks.

## Knowledge base

hail reads `organization_knowledge`. No row means the knowledge base is off. This change adds two columns:

| Column            | Type                          | Rule                                                                                                                                                                                 |
| ----------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `max_total_bytes` | bigint NULL                   | > 0. The most live knowledge data the workspace may hold, counted like the global cap (sum of its `knowledge_documents.stored_bytes`). NULL: no workspace cap (staff and older rows) |
| `source`          | text NOT NULL default `staff` | `plan` or `staff`. The billing site writes and deletes only `plan` rows and never touches a `staff` row                                                                              |

hail checks `max_total_bytes` where it checks the global cap: upload links, upload bodies and new documents (early refusals, 409), and the ingest worker under the global cap's lock (the document fails with "this workspace's knowledge base is full").

The billing site writes the row from the plan (the sizes come from its plan table, never from hail):

| Event                                 | Billing site writes                                                                                                                                                                                               |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| plan on (`active`)                    | upsert with `source = 'plan'`: `max_total_bytes` (Pro 100 MB, Business 1 GB), `max_file_bytes` 20 MB, `max_documents` and `max_pieces` high enough not to bind before the bytes (CHECK limits: 1,000 and 100,000) |
| payment failed (`past_due`, `unpaid`) | delete the row: the knowledge base is off, files are kept                                                                                                                                                         |
| payment back (`active`)               | upsert the row again                                                                                                                                                                                              |
| subscription deleted                  | leave the row deleted, set `ended_at` on the plan; the file delete below runs                                                                                                                                     |

Deleting files at the period end uses the knowledge base's one delete function (`delete_org_knowledge`). It deletes the files, their rows and the limits row, and is safe to run again. Until a scheduled sweep exists, staff run it with `python -m hailhq.core.knowledge.access delete <org-id>` (knowledge base runbook). The sweep (follow-up, after the knowledge base PR merges) runs it daily for each workspace whose last plan ended `canceled` and that still has knowledge files and no open plan.

The cap on all workspaces together (`GLOBAL_MAX_BYTES` in `hailhq.core.knowledge.access`) still applies on top. Raise it before a workspace needs more than it allows.

## Self-host

- With billing off (`HAIL_BASE_URL` or `HAIL_INTERNAL_SECRET` unset), `plan_allows` answers true and the call cap is off, as today.
- Self-hosters never have plan rows. The shared-key org skips the funds gate, as today.

## Order of work

1. The knowledge base PR merges and deploys (this migration runs after its migration).
2. This PR merges and deploys (hail GHA deploy runs the migration).
3. The billing site's plans and Stripe PR. It writes `organization_plans` and `organization_knowledge`, but **no `plan_allowances` rows**: hail passes use on a month's row, and without the raters raising `used_*`, that row would let use through with no end.
4. The billing site's metering PR: `plan_allowances` rows, the raters that raise `used_*` and write plan rows, the quote fields. Its first allowance rows ship with it, never before.

## Tests

- Migration: upgrade and downgrade; constraints refuse a bad tier, status, a `plan` row with cents, a second open plan.
- `has_funds`: plan on with minutes left and an empty wallet passes for calls; `past_due` fails; texts pass only for `text_countries`; no plan is today's result.
- Cap: no plan or no quote fields gives today's limit and hold; plan minutes extend the limit; two live calls share the plan minutes; recording is paid from credits.
- Number purchase: an included number writes the plan row; over the cap writes the `:over_plan` debit; a number beyond the oldest included ones, or a plan that stopped before a late order finished, is today's charge.
- `plan_allows`: tier table, billing off is allowed.
