# SMS billing model correction — design

**Status:** accepted, not yet implemented
**Date:** 2026-07-15
**Amends:** [`2026-07-06-sms-support-design.md`](./2026-07-06-sms-support-design.md) — Decision 12, the Pricing table, and the `PATCH /numbers/{id}/capabilities` provisioning decision.
**Branch:** `feat/sms-console-ui` (worktrees in both `hail` and `hail-website`)

## Why this spec exists

A max-effort review of `feat/sms-console-ui` surfaced three billing defects. Working
them through revealed that all three share one root cause: **the SMS monthly-fee
model was implemented against semantics the code no longer has.**

- `lib/monthly-fee-rater.ts` gates both fees on `is_pool = FALSE AND 'sms' = ANY(capabilities)`.
  That predicate is correct against the 2026-07-06 spec, where `capabilities` is the
  org's *choice* of enabled channels. But the shipped `enable_sms` treats capabilities
  as **carrier-fixed at purchase** (`numbers.py:187`), and the Twilio search hardcodes
  `sms_enabled=True`, so every acquired number carries `"sms"`. The predicate is
  effectively `is_pool = FALSE`. The column's meaning drifted underneath the predicate.
- The fee only ever writes refs for `currentBillingMonth()`, and its only live caller is
  a billing-page render. The spec called for "a cron-driven job"; no cron exists. An
  unrendered month is never billed.
- That render also runs a platform-wide rater under a global advisory lock, so every
  org's billing page serializes behind every other org's.

Rather than patch the predicate, this spec corrects the model. Two of the three defects
dissolve; they do not need fixes.

## Decisions (locked)

1. **The dedicated-number fee is at cost, per number held, no markup.** An org pays
   exactly what Twilio charges for that number's `(country_code, number_type)`, every
   month, for as long as the org holds it. This replaces the hardcoded
   `dedicated_number_cents_per_month: 115`, which bills a US-local price for every
   number including non-US and toll-free ones that cost Twilio a different amount.
   Supersedes the 2026-07-06 Pricing table's `$2.50/month` (~54% margin).

2. **The fee applies to any dedicated number, on any channel.** Twilio bills Hail for
   a held number whether it is used for voice, SMS, or nothing. Recovering that is not
   a markup. The pricing copy's "No monthly rental for voice numbers" refers to the
   **shared voice pool** (`is_pool = TRUE`), which really is free; it is reworded so it
   cannot be read as covering a dedicated number.

3. **10DLC is not a fee. It is absorbed into the US segment rate.** A2P 10DLC is a
   US-destination regime (The Campaign Registry serves US carriers); Canada does not
   participate and rest-of-world is unaffected. Hail runs **one shared platform-level
   Brand/Campaign** (2026-07-06 Decision 2), so the cost — ~$46 one-time (TCR brand
   $4.50 + standard vetting $41.50) plus ~$1.50–10/mo **total, platform-wide** — does
   not vary per org. A per-org line item for a cost that is not per-org presents margin
   as a pass-through.

   **This amends 2026-07-06 Decision 12** ("itemized to customers as a small, separate
   line item... matching how Twilio/Vonage/Bandwidth itemize 10DLC too"). That analogy
   does not hold: those providers itemize because *each of their customers has their own
   brand/campaign* — a genuine per-org pass-through. Hail's does not.

   The decisive argument for absorbing it, confirmed by research (below): **the real
   recurring 10DLC cost is a per-message carrier pass-through that scales with volume**
   (~0.42¢/segment), not the flat TCR campaign fee ($1.50–10/mo platform-wide total,
   which amortizes to zero). A flat $1/mo per org could never track a per-segment cost —
   an org sending 10k US segments/mo incurs ~$42 of carrier fees, not $1. The old model
   therefore billed 10DLC *twice*: once correctly inside the per-segment rate (which
   already carries the carrier fee), and once as a flat fee that mapped only to the
   trivial registration cost and was ~pure margin mislabeled "compliance." Folding the
   whole 10DLC concept into the per-segment rate is the only structure that matches how
   the cost is actually incurred. It also prices the shared-campaign externality
   (throughput/reputation) that `core/hailhq/core/abuse_monitor.py` exists to contain.

4. **No US rate change; 2.5¢ holds at ~50% margin.** Verified against Twilio's own US
   pricing page (`twilio.com/en-us/sms/pricing/us`, fetched 2026-07-15; see Research):

   | Layer | Outbound | Inbound |
   | --- | --- | --- |
   | Twilio base | 0.83¢ | 0.83¢ |
   | Carrier pass-through, blended | ~0.42¢ | ~0.44¢ |
   | **All-in COGS** | **~1.25¢** | **~1.27¢** |

   At 2.5¢ that is **~50% / ~49% gross margin** (outbound / inbound). The 2026-07-06
   spec's ~1.23¢ figure is **confirmed**, marginally conservative. So absorbing 10DLC
   means **deleting the fee and re-documenting why 2.5¢ is the number** — no
   customer-visible rate moves. `ca` (3.5¢) and `row` (20¢) are untouched; 10DLC never
   applied to either.

   Two caveats carried forward, not blockers: (a) 50% is the floor of "healthy" for CPaaS
   resale, and a Verizon-heavy inbound mix (Verizon inbound carrier fee is 0.70¢, worst
   case ~1.53¢/segment all-in) trims it a point or two; (b) carrier fees drift — T-Mobile
   raised them Jan 2026 — which is a second reason the `a2p_10dlc[]` and per-segment
   figures belong in a staleness-checked `costs/` file rather than a hardcoded constant.
   The competitor check (Research appendix) confirms 2.5¢ is not under-market: it is ~2×
   the priciest major CPaaS all-in and well below bundled/retail SMS tools.

5. **Releasing the number is how an org stops paying.** Not a capability toggle. The
   two-way `PATCH /numbers/{id}/capabilities` from the 2026-07-06 provisioning section
   is **deferred**: its only load-bearing purpose was gating the fee, and with (3) the
   fee is gone and nothing bills on `capabilities` at all. It remains a reasonable future
   product feature ("pause SMS, keep the number"), but it is not a fix for anything and
   the repo's own tenet is "No abstractions without two concrete uses."

6. **`costs/telephony.json` is the source of truth for what Hail bills for a number.**
   Not a constant, and not a live Twilio API read. It carries `numbers[]` (price per
   country/type) and `a2p_10dlc[]` (the carrier + TCR fees behind Decisions 3–4; seed
   values in the Research appendix). It is
   schema-validated on every PR, staleness-checked weekly, publicly rendered, and its
   price history is `git log -p costs/telephony.json` — the mechanism its own README
   already documents. A price change becomes a reviewable PR rather than a silent bill
   change.

   **Scope:** `numbers[]` + `a2p_10dlc[]` only. Per-segment SMS wholesale is
   deliberately **excluded** — publishing it next to the public 2.5¢/3.5¢/20¢ rates
   would expose the exact SMS margin. This matches the existing posture for voice: 9¢/min
   never exposes LLM/TTS COGS even though `llm.json` is public.

7. **A scheduled GitHub Actions workflow drives the billing run**, copying the existing
   `.github/workflows/costs-stale.yml` pattern (`on: schedule` + `workflow_dispatch`).
   Not Vercel Cron: `hail-website` has no workflows and no `vercel.json`, and GHA is
   already the house pattern for scheduled work in this repo.

8. **The rater carries a durable backlog.** It enumerates every unbilled month per
   number since acquisition, not just the current month. This is what makes a missed run
   *late* rather than *lost*, and it mirrors what already makes the usage rater safe
   (a push trigger plus a `WHERE priced_at IS NULL` backlog).

## What does NOT change

- The `us`/`ca`/`row` classifier and the tiered per-segment rates (2.5¢ / 3.5¢ / 20¢).
- `enable_sms` — it still provisions the org's Messaging Service for future send routing.
  It simply stops being a billing signal.
- `capabilities` staying carrier-fixed. Now harmless: nothing bills on it.
- `rateUnpricedUsage()` and its billing-page call. Pre-existing, bounded (`LIMIT 500`,
  `FOR UPDATE SKIP LOCKED`), and out of scope.

## Data model

**No migration.** Every column needed already exists:

| Column | State today | Use |
| --- | --- | --- |
| `phone_numbers.country_code` | written, `NOT NULL` | lookup key into `telephony.json` |
| `phone_numbers.number_type` | written, `NOT NULL` | lookup key into `telephony.json` |
| `phone_numbers.acquired_at` | **declared, never written** | backlog anchor |
| `phone_numbers.released_at` | **declared, never written** | stops the fee |
| `phone_numbers.provisioning_state` | written (`'active'` only) | `'released'` on release |

`acquired_at` is NULL on every existing row. Rather than a backfill migration, the rater
anchors on `COALESCE(acquired_at, created_at)` — `created_at` is `NOT NULL` with a
`now()` server default, so it is a sound floor for pre-existing rows. New acquisitions
populate `acquired_at` explicitly.

The invented `monthly_cost_cents` column from an earlier draft is **not needed**:
`(country_code, number_type)` already resolves the price, and `account_credits.amount_cents`
already records what was actually billed, so history is preserved without it.

## `costs/telephony.json`

`costs/` is currently **model-shaped**, not provider-shaped. Extending it is the bulk of
this work:

```
costs/telephony.json          NEW   { version, license, numbers[], a2p_10dlc[] }
costs/schema/telephony.schema.json
                              NEW   modeled on stt.schema.json
costs/README.md               EDIT  reframe from "AI model providers" to provider COGS;
                                    the "common envelope { version, license, models[] }"
                                    claim no longer holds for every file
.github/workflows/costs-validate.yml
                              EDIT  add a validate step; extend the hardcoded
                                    `for f in costs/llm.json costs/stt.json costs/tts.json`
                                    cross-field loop
scripts/costs/check-stale.mjs EDIT  see below — load-bearing
web/lib/costs.ts              EDIT  export `telephony`
web/components/categories/telephony-section.tsx
                              NEW   copy the STT/TTS section pattern
web/app/(dispatch)/page.tsx   EDIT  render the section; `totalModels` math
```

Every row keeps the existing provenance fields — `last_verified`, `last_changed_at`,
`verification_method`, `verified_by`, `source_url` — so the "at cost" claim is auditable
against Twilio's published pricing.

> **`check-stale.mjs` is load-bearing and will silently skip this file.** It globs the
> data directory but line 38 reads:
>
> ```js
> if (!Array.isArray(data?.models)) {
>   console.error(`skip: ${file} has no \`models\` array`);
> ```
>
> A `telephony.json` with `numbers[]` is skipped by the weekly staleness check — the exact
> mechanism that keeps prices honest, and the one thing the "at cost" claim depends on.
> **Without this the file rots unnoticed while the invoice keeps citing it.**
>
> Fix: give each file an explicit row-array key (a `ROW_KEY` map, `{ llm: "models",
> telephony: "numbers", … }`), so an unrecognized file is a **hard error rather than a
> `skip:` line**. The current `skip`-and-continue behaviour is what would let this pass
> silently. Two tests required: `telephony.json` is staleness-checked, and an unknown
> data file fails the run instead of being skipped.

## Billing mechanics

**Predicate** — `capabilities` drops out entirely:

```sql
SELECT id, organization_id, country_code, number_type,
       COALESCE(acquired_at, created_at) AS since
  FROM phone_numbers
 WHERE is_pool = FALSE
   AND provisioning_state = 'active'
   AND released_at IS NULL
```

**Flow:**

```
GHA schedule (monthly + workflow_dispatch)
  -> POST /api/internal/monthly-fees/rate      (exists; signature-verified; zero callers today)
       -> fetch costs/telephony.json           (canonical raw URL, once per run)
            fetch fails -> abort run, log, bill nothing
                           the backlog recovers on the next run: late, never lost
       -> for each held number:
            for each month from COALESCE(acquired_at, created_at) -> now:
              price = telephony.numbers[(country_code, number_type)]
              price missing -> skip this number, log loudly, do NOT default a price
              INSERT debit ... WHERE NOT EXISTS (org, ref)    -- existing idempotency guard
```

**Deleted:** the `ten_dlc_compliance` fee, its query, its INSERT loop, and its
idempotency key branch. `MONTHLY_FEES_CENTS` goes entirely — both
`ten_dlc_compliance_cents_per_month` (Decision 3) and `dedicated_number_cents_per_month`
(Decision 1, now sourced from `telephony.json`).

**`getPublicFacingMonthlyFees()` is removed, not reshaped.** Per-country pricing means a
single scalar fee no longer exists, so its four callers each need a different answer. It
is replaced by one module, `lib/telephony-costs.ts`, which fetches and caches
`telephony.json` (Next cached `fetch` with revalidate) and exposes:

- `numberPriceUsdPerMonth(countryCode, numberType)` — the exact price for one number.
- `cheapestNumberPriceUsd()` — the "from $X" figure for marketing surfaces.

| Caller | Today | After |
| --- | --- | --- |
| `lib/monthly-fee-rater.ts:78` | `fees.dedicatedNumberUsdPerMonth` | `numberPriceUsdPerMonth(row.country_code, row.number_type)` |
| `app/console/sms/page.tsx:24` | flat `$2.15` in the acquire confirm | the **price map**, passed to `NumbersPanel` — see below |
| `app/(marketing)/pricing/page.tsx:35` | `$1.15/mo + $1.00/mo` | "from `cheapestNumberPriceUsd()`/mo — you pay what the carrier charges", linking `/costs` |
| `app/components/CostComparison.tsx:30` | `$2.15/mo` SMS number line | same "from $X, at cost" framing |
| `lib/__tests__/private-rates.test.ts:49,52` | pins `1.15` / `1.0` | move to `telephony-costs` tests; the 10DLC assertion is deleted outright |

One module, one fetch path, one source — used by the rater, the console, and the
marketing pages alike.

**The acquire confirm needs the map, not a scalar.** `NumbersPanel` is a client component
(`"use client"`) holding the `countryCode` the user is typing, while `page.tsx` is a
server component. A single price cannot reach it. The server component passes the
`numbers[]` price map as a prop, and the panel resolves the price for the *currently
selected* `(country, type)` when it builds the confirm text — so an org acquiring a UK
toll-free number is quoted the UK toll-free price, not a US local one. If the selected
pair is absent from the map, the acquire button is disabled with "we don't have a price
for that country yet" rather than quoting a wrong figure.

**The billing page stops calling `rateMonthlyFees()`.** That single change resolves the
third defect: no platform-wide scan on a user render, no viewer serialization, and the
global advisory lock becomes harmless because a scheduled job is the only caller. The
lock stays — the internal route is publicly reachable and concurrent runs can overlap —
but it no longer sits on anyone's page load.

**Error handling:**

- Missing price for a `(country, type)` → skip that number, log; never default or guess.
  Under-billing silently is the failure mode this whole spec exists to eliminate.
- `telephony.json` fetch failure → abort the run. Correct because of the backlog.
- Release fails at Twilio → do not write `released_at`; the fee correctly continues.

## Release path

```
hail        POST /numbers/{id}/release
              -> provider.release_number()          (exists, zero callers today)
              -> released_at = now(), provisioning_state = 'released'
              -> guard: 404 for another org's number; idempotent if already released
              -> regenerate openapi/openapi.yaml IN THE SAME PR (repo invariant)

hail-website console Release control, behind confirm() — matching SuppressionPanel's
              existing pattern for destructive actions
```

The rater's `released_at IS NULL` clause is dead code until this lands, which is why it
is in scope: without it, the branch ships a recurring charge no customer can stop.

## Copy changes

All rates and fees already render from `private-rates.ts`, so most copy follows the
constants. Explicit edits:

- `pricing/page.tsx` — "No monthly rental for voice numbers" → the pool is free; a
  dedicated number is at cost, any channel. Remove the $1.00/mo 10DLC fee from the FAQ.
- `pricing/page.tsx` `included[]` — "The paperwork" entry: 10DLC registration is genuinely
  included now (absorbed into the US rate), so this becomes true rather than contradictory.
- `CostComparison.tsx` — the SMS number line reflects at-cost, single fee.
- `private-rates.ts` — document that the 2.5¢ US rate absorbs 10DLC amortization, and
  why `ca`/`row` do not.
- Link the public `/costs` telephony table wherever "at cost" is claimed.

## Deploy ordering

Cross-repo. `costs/telephony.json` lives in `hail` (public, GHA-deployed on push to
`main`); the rater lives in `hail-website` (Vercel, git integration on `master`). The
rater fetches the file from its canonical raw URL, so:

1. **`hail` first** — `telephony.json` must be reachable before any rater run reads it.
2. `hail-website` second.
3. The GHA schedule is enabled last, once both are live.

A rater deployed before the file exists aborts every run and bills nothing — recoverable
by the backlog, and the reason the abort-don't-default rule above matters.

## Testing

- `classify_pricing_tier` — already covered by this branch's regression tests (foreign
  NANP → `row`, Canadian relief NPAs → `ca`, US territories → `us`).
- `telephony.json` validates against its schema (wired into `costs-validate.yml`).
- `telephony-costs.ts`: `numberPriceUsdPerMonth` resolves a known pair, returns null (not
  a default) for an unknown one; `cheapestNumberPriceUsd` picks the true minimum across
  `numbers[]` rather than assuming US local is cheapest.
- Console: an unpriced `(country, type)` disables acquire instead of quoting a figure.
- **`check-stale.mjs` does not skip `telephony.json`** — asserts the generalization above.
- Rater: a US local, a UK toll-free, and a voice-only number each bill their own
  `telephony.json` price — the case the hardcoded `115` gets wrong today.
- Rater: a number held for 3 unbilled months produces 3 debits in one run (backlog).
- Rater: a re-run produces zero new debits (existing `NOT EXISTS` guard).
- Rater: `released_at IS NOT NULL` → no debit.
- Rater: unknown `(country, type)` → skipped and logged, no debit, no default.
- Rater: `telephony.json` unreachable → run aborts, zero debits, next run catches up.
- `monthlyFeeIdempotencyKey` — this branch's tests already pin the production composite
  `org:number` shape.
- Release: releases at the provider, sets both columns, is idempotent, 404s cross-org.
- No test asserts a `ten_dlc_compliance` ref survives.

## Out of scope

- `PATCH /numbers/{id}/capabilities` and a console SMS toggle (Decision 5).
- Per-org 10DLC registration — already "explicit future work" in 2026-07-06.
- A `UNIQUE INDEX ON account_credits(organization_id, ref)`. It would let
  `ON CONFLICT DO NOTHING` replace the advisory lock, but the lock stops being a problem
  once the rater leaves the render path, and the index needs a migration in `hail` for a
  table only `hail-website` writes. Worth doing; not needed for this.
- `rateUnpricedUsage()`'s billing-page call.
- MMS (deferred in 2026-07-06 Decision 11); the homepage MMS/shortcode copy claims were
  already corrected on this branch.

## Research appendix — US SMS COGS

Deep-research pass, 2026-07-15. Figures verified 3-0 against primary sources unless noted.
This is the citation trail for Decisions 3 and 4 and the seed data for the `a2p_10dlc[]`
and number-price rows in `costs/telephony.json`.

- **Twilio US base, long code / 10DLC:** $0.0083/segment, outbound and inbound alike.
  Source: `twilio.com/en-us/sms/pricing/us` and `twilio.com/en-us/pricing/messaging`.
- **Per-message carrier pass-through, outbound:** AT&T $0.0035, T-Mobile $0.0045,
  Verizon $0.0045, US Cellular $0.005, all others $0.004. Source: `twilio.com/.../sms/pricing/us`.
- **Per-message carrier pass-through, inbound:** AT&T $0.0035, T-Mobile $0.0025,
  Verizon $0.007, US Cellular $0.0025 — carrier fees apply to inbound too, and Verizon
  inbound exceeds its outbound. Source: same.
- **Blended carrier fee** at US market share (T-Mobile ~37%, Verizon ~35%, AT&T ~28%):
  ~0.42¢ outbound, ~0.44¢ inbound → all-in ~1.25¢ / ~1.27¢.
- **TCR recurring campaign fee:** $1.50/mo (low-volume mixed) to $10/mo (standard),
  platform-wide **total**, not per-org — amortizes to ~0/segment at volume. Source:
  Telnyx 10DLC fees page (Twilio's own product page does not publish this — verified 3-0).
- **Competitor all-in, US (base + identical ~0.42¢ carrier pass-through):** Telnyx ~0.82¢
  (base $0.004), Plivo ~1.19¢ (base $0.0077), Sinch ~1.20¢ (base $0.0078), Vonage ~1.22¢
  (base ~$0.008), Twilio ~1.25¢ (base $0.0083). Sources: each provider's US pricing page
  (Plivo, Telnyx fetched directly; Vonage, Sinch via search). Carrier fee is a regulatory
  pass-through, so it is the same across providers.
- **Market position:** Hail's 2.5¢ flat all-in is ~2× the most expensive major (Twilio)
  and ~3× the cheapest (Telnyx) — i.e. **not under-market**; it is the ~50% bundle margin
  seen from the competitor side. These CPaaS are Hail's wholesale/supplier tier, not retail
  peers: a developer using them directly eats the integration overhead Hail bundles. True
  bundled/retail SMS tools charge 2–5¢+, so 2.5¢ is not over-market either.
- **Supply-side lever (out of scope, procurement not pricing):** Telnyx base $0.004 vs
  Twilio $0.0083 — sourcing the base layer from Telnyx would cut all-in COGS ~1.25¢ →
  ~0.82¢, lifting margin at the unchanged 2.5¢ from ~50% to ~67%. The ~0.42¢ carrier layer
  is fixed regardless of supplier.
- **Staleness flag:** T-Mobile raised A2P pass-through fees effective Jan 19, 2026
  (source: Telgorithm; unverified, search-limit) — the live Twilio figures above already
  reflect any such change, but the drift is real and argues for the `costs/` staleness check.
