# Multi-country number pricing & acquisition — design

**Status:** accepted, not yet implemented
**Date:** 2026-07-17
**Builds on:** [`2026-07-15-sms-billing-model-correction-design.md`](./2026-07-15-sms-billing-model-correction-design.md) (Plan 1 shipped `costs/telephony.json` + the `/costs` telephony surface; Plan 2 shipped the at-cost monthly-fee rater keyed on `(country_code, number_type)`).
**Repos:** `hail` (costs data, sync, acquire API, `/costs` render) and `hail-website` (console acquire UI, rater — both already read `telephony.json`).

## Why this exists

Today `costs/telephony.json` carries only US rows, and the monthly-fee rater **skips-and-logs** any held number whose `(country_code, number_type)` isn't in the file — a silent revenue leak. The acquire API accepts **any** country with no price guarantee. And the earlier UI assumed "every number does both calls and texts," which is false: verified against Twilio's own numbers dataset, Sweden is SMS-only (no voice number exists), Norway splits voice and SMS across two numbers, France/Germany/Ireland/Australia local numbers are voice-only, and MMS exists only in US/Canada.

This spec makes number pricing **multi-country, capability-accurate, and leak-proof**: `telephony.json` becomes the single source of prices *and* capabilities *and* the acquire allow-list, populated from Twilio's own data, and the console gets an honest, opinionated picker.

## Decisions (locked)

1. **`telephony.json` is the allow-list.** Only a `(country_code, number_type)` present in the file (with a price) can be acquired — enforced in the hail acquire API, not just the console. "Price every number" is then true by construction: the rater can never meet a held number it can't price. Adding a market = adding its rows.

2. **Capabilities are first-class data.** Each `numbers[]` row carries `voice`, `sms`, `mms` booleans alongside the monthly price. These are read from Twilio's authoritative dataset, never assumed. The UI, the acquire recommendation, and the `/costs` page all read them.

3. **A hail-side sync populates `telephony.json` from Twilio.** Coverage is broad (every country Twilio sells numbers in — ~65 countries / ~106 number types today), not hand-curated. The sync is the only writer of `numbers[]`; humans review its PR.

4. **Whole-month, at-cost billing is unchanged** (from the 2026-07-15 spec). The rater already prices per `(country_code, number_type)`; it simply gains coverage. Capability fields do **not** affect billing — a held number is billed regardless of what it can do.

5. **The console acquire UX is capability-first and honest.** The user toggles what the number must do (**Calls** and/or **SMS**, independent, both on by default); the picker recommends the cheapest number in each country that satisfies the toggles, and **greys out** (never hides) countries that can't. Number-type detail (local/mobile/freephone) is tucked into an "Options" disclosure with plain-English explainers. Approved design: artifact preview built and signed off.

6. **The API does not guess a number type it can't fulfil.** The console sends an explicit `(country_code, number_type)`; the API validates it against the allow-list. The "recommend the cheapest matching number" logic lives in the UI (and is derivable from the file), not as hidden API magic.

7. **One number per acquisition.** Each acquire action buys exactly one number. In a **split country** (calls and SMS live on different number types — e.g. Norway: voice on `local`, SMS on `mobile`), full coverage means holding **two** numbers, acquired as two separate steps. The UI recommends the SMS number and plainly states a second number is needed for calls; it does not bundle two acquisitions. **This behaviour must be surfaced in user-facing help/docs** so it isn't a surprise. Auto-bundling two acquisitions is possible later, out of scope.

## Data model — `costs/telephony.json`

Extend each `numbers[]` row with capability booleans and a stable identity. Current row:

```json
{ "country_code": "US", "number_type": "local", "display_name": "US local (10DLC long code)",
  "usd_per_month": "1.15", "last_verified": "...", "last_changed_at": "...",
  "verification_method": "manual-confirmed", "verified_by": "r13i", "source_url": "..." }
```

New shape (added fields **bold** in prose): `country_code` stays ISO-3166 alpha-2 (`"SE"`), `number_type` ∈ `local|national|mobile|toll_free`, plus **`voice`**, **`sms`**, **`mms`** (booleans), and **`dial_code`** (string, e.g. `"46"`, for display). `usd_per_month` stays a decimal string. Provenance fields stay; for synced rows `verification_method` becomes **`"carrier-sync"`** and `source_url` is the Twilio dataset URL.

`a2p_10dlc[]` is unchanged (US-only, from the 2026-07-15 spec).

**Schema** (`costs/schema/telephony.schema.json`): add `voice`/`sms`/`mms` (required booleans) and `dial_code` (required string) to the `number` def; add `"carrier-sync"` to the `verification_method` enum. A row with neither `voice` nor `sms` true is invalid (a number must do something) — enforce with an `anyOf`.

> A frozen snapshot of the parsed dataset used to design this (65 countries, 106 rows) is the acceptance reference for the sync's output shape. Sweden = one `mobile` row, `voice:false, sms:true`. That row is the canary: if the sync ever emits Sweden as voice-capable, it's wrong.

## The sync — `scripts/costs/sync-telephony.mjs`

A Node script (mirrors the existing `scripts/costs/check-stale.mjs` conventions) that:

1. **Fetches Twilio's authoritative numbers dataset.** Primary source: Twilio's `SiteNumbersPricing` CSV feed (the machine-readable form behind the pricing pages) — columns include `ISO, Country, Country Code, Phone Number Type, Voice Enabled, SMS Enabled, MMS Enabled, Phone Number Price / month`. This is what the capability research verified against.
2. **Maps** each row → a `numbers[]` entry: ISO→`country_code`, `Country Code`→`dial_code`, type-name→`number_type` (`Local→local, Mobile→mobile, Toll Free→toll_free, National→national`), `Yes/No`→booleans, price→`usd_per_month` (decimal string). Drops rows with neither voice nor sms.
3. **Writes** `costs/telephony.json`: replaces `numbers[]`, preserves `a2p_10dlc[]`, stamps provenance (`verification_method: "carrier-sync"`, `source_url`, `last_verified` = run date; `last_changed_at` only bumped for rows whose price/capability actually changed, so staleness stays meaningful).
4. **Validates** its own output against the schema before writing; fails loudly on schema violation.

**Trigger:** a scheduled GitHub Actions workflow (copying `costs-stale.yml`'s `on: schedule` + `workflow_dispatch`) that runs the sync and **opens a PR** with the diff — a human reviews price/capability changes before they go live. Not an auto-commit: pricing changes to a money surface deserve eyes.

**Risks / flags:**
- The CSV feed URL contains an opaque hash that may rotate. Mitigation: the sync resolves the current feed URL from Twilio's pricing page rather than hardcoding it, and fails loudly (no silent empty write) if it 404s. Long-term option: Twilio's Pricing API (`pricing.twilio.com/v1/PhoneNumbers/Countries`) with a CI-secret credential — more stable, but needs auth and a separate capabilities source. Start with the CSV; note the API as the hardening path.
- **Never write an empty or partial `numbers[]`.** If the fetch yields fewer than a sanity floor (e.g. < 40 countries), abort and keep the committed file — a truncated sync must not silently shrink the allow-list and make held numbers unbillable.

## Acquire API — allow-list enforcement (hail)

A new core module `hailhq.core.telephony_catalog` loads the committed `costs/telephony.json` (bundled into the API image) and exposes:
- `is_acquirable(country_code, number_type) -> bool`
- `price_usd_per_month(country_code, number_type) -> Decimal | None`
- `capabilities(country_code, number_type) -> {voice, sms, mms} | None`

`POST /numbers` (acquire) validates the requested `(country_code, number_type)` with `is_acquirable`; if absent, **422** with a clear message ("we don't offer a `<type>` number in `<country>` yet"). This closes the raw-API leak the console already guards. `NumberAcquireRequest` already carries `country_code` + `number_type`; the guard is the only acquire-endpoint change.

**Required schema change — the `national` number type (DECIDED: include).** Twilio offers a `national` type for several countries (Japan, Brazil, Romania, Czech Republic), and we are including it — it's real coverage for real markets (e.g. Japan's main number is `national`). But hail's `phone_numbers.number_type` CHECK constraint and the `NumberType` literal only allow `local|mobile|toll_free`. So this needs a **migration** extending the CHECK constraint to `('local','mobile','toll_free','national')` and adding `'national'` to `NumberType`, plus confirming the provider adapter can search Twilio for a `national` number. (This is a genuine migration — unlike the 2026-07-15 rater work, which needed none.)

> The catalog reads the same file the rater and UI use, so the three can never disagree about what's acquirable/priced.

## Console acquire UI (hail-website)

Replace the bare country-code input in `app/console/sms/NumbersPanel.tsx` with the approved picker (built and signed off as an artifact; translate to React):
- **Two independent capability toggles** — `Calls`, `SMS` — both on by default. They filter what a number must do.
- **Country list** from `telephony.json`, one row per country, showing a `Calls` badge and an `SMS` badge (green if the country has any number with that capability, dashed/struck if not), and the cheapest price for a number matching the active toggles. **Price / A–Z** sort; search.
- **Countries that can't satisfy the toggles are greyed** (dimmed, non-selectable) in a trailing "can't do X here" group — visible, not hidden, so the missing badge explains why.
- **Confirm dock** on select: the recommended number, its real capabilities, the recurring monthly cost, and honest caveats ("billed monthly until you release it"). An **Options** disclosure lists every number type for that country with a plain-English explainer, its true capabilities, and price; the cheapest matching type is pre-selected ("Best pick").
- **Split countries** (both toggles on, but no single number does both — e.g. Norway: voice on `local`, SMS on `mobile`): the picker recommends the **SMS** number and the dock states plainly that calls need a **second** number ("add a local number for calls — $X/mo"). **v1 acquires one number per action**; it does not bundle two acquisitions. The user acquires the second number as a separate step if they want it. (Auto-bundling two acquisitions is a possible later refinement, out of scope.)
- On confirm → `acquireNumberAction(countryCode, numberType)` (extend the existing action to pass the type).

The picker reads capabilities from the existing `fetchTelephonyData()` (now returning the richer rows). The pure helpers (`numberPriceUsdPerMonth`, a new `recommendNumber(country, {calls, sms})`) live in the client-safe `lib/telephony-costs-shared.ts`.

## `/costs` render (hail)

Extend `web/components/categories/telephony-section.tsx` (Plan 1) to render all countries with their capabilities — add `Calls`/`SMS`/`MMS` columns (or capability chips) and let the table scale to ~65 countries (it already sorts by price). The public page then honestly shows what each country's number can do, at cost.

## Rater (hail-website) — essentially unchanged

`lib/monthly-fee-rater.ts` already prices held numbers by `(country_code, number_type)` from `telephony.json` and bills the durable backlog. It needs **no logic change** — broader coverage means fewer skipped numbers. One check: confirm the rater still ignores capability fields (it should; billing is per held number). The `numberPriceUsdPerMonth` lookup keys on `(country_code, number_type)`, which stays stable.

## Testing

- **Schema**: `telephony.json` validates (capabilities required; a no-capability row rejected). Wired into `costs-validate.yml`.
- **Sync**: given a captured Twilio CSV fixture, the mapper emits the expected `numbers[]` (Sweden mobile → `voice:false,sms:true`; UK toll-free → `voice:true,sms:false`; US local → all true). Empty/short fetch → aborts, file unchanged. Bad type name → dropped/flagged.
- **`telephony_catalog`** (hail): `is_acquirable` true for a listed pair, false for an unlisted one; `capabilities`/`price` correct; missing file → loud failure, not silent allow.
- **Acquire API**: acquiring a listed `(country,type)` succeeds; an unlisted one → 422; the console-blocked case and the raw-API case both covered.
- **UI helpers**: `recommendNumber` returns the cheapest type matching `{calls,sms}`; returns null when the country can't satisfy them (drives the greyed state); a split country (voice+sms on different types) is handled.
- **Rater**: a held number in a newly-covered country bills its telephony price (the case that silently skipped before).
- `check-stale.mjs` staleness-checks the (now larger) `numbers[]` — already generalized in Plan 1.

## Deploy ordering

1. **hail**: the `national` migration (CHECK constraint + `NumberType`, run before deploy) + telephony schema + sync + a first synced `telephony.json` (broad coverage) + `telephony_catalog` + acquire guard + `/costs` render, on a fresh branch off `main` (the old `feat/sms-console-ui` is merged/stale). The synced file must be on `main`'s raw URL before hail-website builds against it (same constraint the 2026-07-15 spec hit).
2. **hail-website**: the picker + `acquireNumberAction` type param, continuing on its branch.
3. Enable the sync's scheduled workflow last.

## Out of scope

- **Number release** — still deferred (Plan 3 from the 2026-07-15 spec, unbuilt). The "recurring charge with no self-serve release" caveat stays until it lands; the UI states it honestly.
- **MMS sending** — data carries the `mms` flag, but MMS as a product feature is out of scope.
- **Alphanumeric sender ID** for countries with no textable number (France, etc.) — those countries simply don't appear under the SMS toggle; wiring sender-ID sends is separate future work.
- **Per-country A2P/regulatory bundles** (Twilio "Address Required" / regulatory bundles) — a real provisioning gate for many EU numbers, but a separate concern from pricing/capability; flagged for a follow-up.
- **Twilio Pricing API** migration (vs the CSV feed) — noted as the hardening path, not v1.
