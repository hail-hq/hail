# Inbound email billing — design

Status: approved
Owners: r13i
Repos: `hail/` (api, core), `hail-website/` (rater, ledger copy)

## Goal

Charge **$0.01 per inbound email received**, once, regardless of what happens
to it downstream (forward, webhook, both, neither). Close the cost hole where a
zero-balance org consumes paid SES sends via forwarding for free.

## Decisions (locked)

1. **Flat $0.01 per message, both directions.** One charge per received message
   (not per forward/webhook), and outbound is **also flattened to 1¢ per send**
   regardless of recipient count (today outbound bills per-recipient and rounds
   up). Both channels: `units=1 × 1.0 cent`. Simpler to explain and to reason
   about; the small revenue change on multi-recipient sends is intentional.
2. **Metered for every newly-created inbound row**, including spam/virus-suppressed
   ones — we still received, parsed, and stored the message. SES redeliveries
   (`created=False`) are **not** charged.
3. **Out-of-credit behavior:** never lose mail. The row + raw MIME are always
   persisted and the charge is always written (balance may go negative).
   Forwarding (which spends money on SES sends) is **suppressed** with a new
   suppression reason `insufficient_funds`; webhook fan-out still fires so the
   tenant is notified. Outbound `POST /emails` gating is unchanged.
4. **Distinct ledger channel `email_inbound`** (not the existing `email`), so the
   customer-facing ledger label and qty semantics ("N received" vs "N sends")
   stay honest and the two rates can diverge later.
5. **Self-host** (sentinel "Self-hosted" org, shared-key auth) is never metered —
   same as outbound; the rater only debits orgs that carry an `account_credits`
   ledger, so no special-casing is required (verify in the plan).

## Architecture

```
/internal/ses-events handler
  → ingest_inbound(...)                      # core: persist + fan-out
      • funds_check(db, org) before forwards # NEW injected callable
        - not funded + forward_to set →
            skip forwards, reason=insufficient_funds, emit suppressed event
      • returns created_email_ids[]           # NEW: only newly-created rows
  → for each created id:                      # api: meter (mirrors outbound)
        write_usage_event(channel='email_inbound', units=1,
                          ref=f'email_inbound:{id}')
  → rating happens via the existing rater (opportunistic pass + cron)
        → debit to account_credits, label "Inbound email", qty "N received"
```

Metering lives in the **API layer** (the `/internal/ses-events` handler), exactly
as outbound metering lives in `POST /emails` — `core` never writes
`usage_events` or knows about pricing.

## Changes

### core (`hail/`)

- `core/hailhq/core/email_ingest.py`
  - `IngestResult` gains `created_email_ids: list[UUID]` (the existing
    `email_ids` keeps current semantics — includes replays — so callers that
    only need "what exists" are unaffected; metering uses the new field).
  - `ingest_inbound(...)` gains an injected `funds_check: Callable[[AsyncSession, UUID], Awaitable[bool]] | None = None`. When `None`, treated as funded (keeps existing tests and ingest-only contexts working).
  - Forward guard becomes: `created and suppress is None and not over_cap and domain.inbound_enabled and forward_enqueue is not None and <funded>`, where `<funded>` is `True` when `funds_check is None` else `await funds_check(db, domain.organization_id)`.
  - When forwarding is configured (`domain.forward_to`) but the org is **not**
    funded: append `insufficient_funds` to this row's reasons (so the existing
    Task-8 machinery emits `email.received.suppressed` with that reason). Do
    **not** emit the reason for orgs that aren't forwarding at all.
- `core/hailhq/core/billing.py` — `has_funds(db, org_id)` already exists; reuse
  it as the `funds_check` argument from the API layer.
- Suppression-reason set is now `{forward_loop, forward_rate_limit, inbound_rate_limit, insufficient_funds}`.

### api (`hail/`)

- Extract the inline `_write_usage_event` from `api/hailhq/api/routes/emails.py`
  into a shared helper (`api/hailhq/api/usage.py::write_usage_event(db, *, organization_id, channel, units, ref)`), and call it from both `POST /emails`
  (unchanged behavior, `channel='email'`) and the ses-events handler
  (`channel='email_inbound'`). Keep the rater-ping behavior identical to outbound.
- `api/hailhq/api/routes/internal/ses_events.py`
  - Pass `funds_check=has_funds` into `ingest_inbound`.
  - After ingest, loop `result.created_email_ids` and call
    `write_usage_event(... channel='email_inbound', units=1, ref=f'email_inbound:{id}')`.

### hail-website

- `lib/private-rates.ts` — add `email_inbound_cents_per_message: 1.0` to
  `RATES_CENTS_PER_UNIT`; extend `UsageChannel` to include `"email_inbound"`;
  add the case to `rateUsageCents` (`units * email_inbound_cents_per_message`,
  ceil already applied). `units` is always 1, so each message → 1 cent.
- `lib/usage-rater.ts` — map `channel='email_inbound'` through the new rate.
- `lib/billing-queries.ts` — ledger label "Inbound email"; qty formatted as
  "N received".
- `app/console/billing/BillingClient.tsx` — channel filter includes inbound
  (fold under the existing "email" filter chip, or add an "Inbound" sub-label —
  decide in plan; simplest is to keep one "Email" chip covering both channels).
- `lib/billing-tiers.ts` — tier estimate copy acknowledges inbound (e.g. "≈5,000
  emails in or out for $50"); no math change since the rate matches.

## Testing

- **core**: ingest with `funds_check` returning False + `forward_to` set →
  forwards suppressed, `insufficient_funds` in `suppressed_reasons`, suppressed
  webhook event carries `reason='insufficient_funds'`, webhook `email.received`
  still fires. Funded path unchanged. `created_email_ids` excludes replays
  (ingest same message twice → one created id).
- **api**: signed `/internal/ses-events` for a funded org → one
  `usage_events(channel='email_inbound', units=1)` row per created message;
  replay → no new usage row. Spam-suppressed inbound → still metered.
- **website**: `rateUsageCents('email_inbound', 1)` → 1 cent; rater turns an
  `email_inbound` usage row into a 1-cent debit labeled "Inbound email".

## Risks / notes

- **Negative balances** are now reachable via inbound (we charge even when broke).
  Acceptable per decision 3; the next top-up settles it. Surfaced honestly in the
  ledger.
- **Funds check adds a balance query per ingested org.** Cheap (indexed sum) and
  only on the inbound path; fine at v1 volumes.
- **`account_credits` is written by the website rater**, not the API — so the
  API-side funds check reads a balance the website maintains. Same coupling
  outbound already relies on; no new boundary.

## Out of scope

- Metering inbound receipt vs forward separately (one charge covers both).
- Charging webhook deliveries or per-attachment storage.
- Per-tenant inbound rate overrides.
