# SMS Console UI & Billing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the tiered SMS pricing for real (the code today still charges the pre-research 1¢/segment placeholder), a recurring monthly-fee billing mechanism for dedicated numbers and the 10DLC compliance fee, and the console surfaces the design spec calls for: SMS in the existing unified activity log, a Sender ID/Numbers/Suppression settings page, and updated public pricing copy.

**Architecture:** This plan spans two repos. A small, tightly-scoped change in `hail/` (the OSS backend) embeds a pricing tier (`us`/`ca`/`row`) into the `ref` of each SMS `usage_events` row, since geo-tiered pricing needs to know which tier applied and the existing schema has nowhere else to carry it. `hail-website`'s `usage-rater.ts` already prices per-row from `ref`/`channel`; this plan extends it to read the tier. A new, parallel `monthly-fee-rater.ts` handles the two flat recurring fees, following `usage-rater.ts`'s idempotent-debit-row shape but keyed by `(org, fee kind, billing month)` instead of a source-event id, since there's no per-cycle event row to "price." The console changes are additive: the unified activity log (`/console/activity`) already has an `"sms"` `ActivityKind` with hardcoded empty stubs — this plan replaces those with real queries; a new `/console/sms` settings page follows the exact `/console/email/domains` page/panel/actions structure already established.

**Tech Stack:** Next.js (App Router) + Postgres via `pool.query` (`hail-website`), FastAPI + SQLAlchemy (`hail`), vitest.

## Global Constraints

- **This plan assumes SMS Outbound Core, Inbound & Compliance, and Numbers & Sender ID are already merged.**
  - **✅ AUDIT 2026-07-15 (supersedes the 2026-07-09 note):** All three prerequisite backend phases are now landed or ready. **SMS Outbound Core** and **Inbound & Compliance** are on `main` (incl. `DELETE /sms/suppressions/{number}`). The **Numbers & Sender ID backend is complete** on branch `feat/sms-numbers-sender-id-v2` — **PR #17, green + `MERGEABLE`**. Once it merges, `main` gains `core/hailhq/core/sender_id.py`, the `sms_sender_identities` table, the `messaging_service_sid` column on `phone_numbers`, and the endpoints `GET/PATCH /sms/sender-id`, `POST/GET /numbers`, `GET /numbers/{id}`, `POST /numbers/{id}/enable-sms`. **Migration head becomes `0033`** (`…0030_sms_to_number_id → 0031_email_attachment_uploads → 0032_phone_number_messaging_service → 0033_sms_sender_identities`), under `api/migrations/versions/`.
    - **Task 5 is now UNBLOCKED** — every server action it defines (`setSenderIdAction`, `acquireNumberAction`, `enableSmsOnNumberAction`, `deleteSuppressionAction`) has a live endpoint. **Before starting Task 5, confirm PR #17 is merged** (or rebase this plan's branch onto it); until then those mutating actions 404.
    - Behavioral notes that affect the UI copy: alphanumeric Sender ID applies only to **no-pre-registration corridors (UK, Germany)**; **US/Canada/India require a dedicated number**, **Australia is forced to the platform default "HAIL"**. Sender ID is **per-org only** — `POST /sms` has no per-message alphanumeric-sender field (`from` is E.164). Number **acquisition is not metered/charged** by the backend today (this plan's Tasks 2–3 add the monthly fees). `enable-sms` provisions the org's Messaging Service but sends do **not** yet route through it — deliverability for US still depends on Hail's shared 10DLC campaign wiring, which is out of scope for both this plan and the Numbers phase.
- **No backend migration in this plan.** Task 1 is pure code (`pricing_tier.py` + a `ref` string change); it adds no Alembic revision. If a later change here ever needs one, it starts at **`0034`** (head is `0033` once PR #17 lands — was `0027` when this plan was first written).
- **Cross-repo**: Task 1 touches `hail/` (a small, additive change to `api/hailhq/api/routes/sms.py`'s existing `write_usage_event` call). Every other task touches `hail-website/`.
- **The SMS pricing in `hail-website/lib/private-rates.ts` is still the pre-research 1¢/segment flat placeholder today** — confirmed by direct read, not assumed (re-verified 2026-07-09: `RATES_CENTS_PER_UNIT.sms_cents_per_segment: 1.0`; voice `9/60_000`, email `0.2`; `rateUsageCents(channel, units)` takes exactly two args today and there is no `getPublicFacingMonthlyFees` export yet). This plan replaces it with the researched tiers (US 2.5¢, Canada 3.5¢, rest-of-world 20¢ flat) and the two monthly fees (dedicated number $1.15/mo — priced at cost, no margin, per a later pricing decision that superseded the design spec's original $2.50/mo figure; 10DLC compliance fee $1.00/mo/org).
- **Pricing tier ≠ Sender ID corridor.** The Numbers & Sender ID plan's `sender_id.py` classifies destinations for alphanumeric-ID eligibility (US/CA/UK/DE/AU/IN + fallback); this plan's pricing tier classifies for billing rate (US/CA/rest-of-world only, three buckets). They are different axes over the same E.164 input — do not try to reuse one classifier for the other's purpose, even though both parse a country prefix.
- **No real cron infrastructure exists in `hail-website` today** — confirmed by direct read: `usage-rater.ts` is invoked by a push-triggered internal endpoint (voicebot calls it right after writing a usage event) plus an opportunistic call on every `billing/page.tsx` render; there is no Vercel Cron config (`vercel.json` has no `crons` array) and no scheduled job of any kind anywhere in the repo. This plan follows the same opportunistic-call pattern for the new monthly-fee rater rather than inventing new scheduling infrastructure — an org that never opens its billing page won't get billed promptly, which is an accepted, pre-existing-pattern limitation (flagged in Risks), not something this plan fixes.
- **`Rates`/`getPublicFacingRates()` cannot silently absorb the new tiers and flat fees** — `Rates` is a per-unit type consumed by `estimateFor`/`buildTiers` for "$X buys N units" math; a flat monthly fee doesn't fit that shape. This plan adds a separate `getPublicFacingMonthlyFees()` accessor rather than overloading `Rates`.

---

## File Structure

```
hail/api/hailhq/api/routes/sms.py             # modified — embed pricing tier in usage_events.ref
hail/core/hailhq/core/pricing_tier.py         # new — US/CA/RoW classifier (distinct from sender_id.py)
hail/core/tests/test_pricing_tier.py          # new

hail-website/lib/private-rates.ts             # modified — tiered sms rates + monthly fee constants
hail-website/lib/__tests__/private-rates.test.ts   # modified
hail-website/lib/monthly-fee-rater.ts         # new
hail-website/lib/__tests__/monthly-fee-rater.test.ts  # new (pure-logic parts only, per repo's DB-test convention)
hail-website/app/api/internal/monthly-fees/rate/route.ts  # new — mirrors usage-events/rate/route.ts
hail-website/app/console/billing/page.tsx     # modified — opportunistic monthly-fee-rater call

hail-website/lib/sms-queries.ts               # new — data layer for Sender ID/Numbers/Suppressions
hail-website/lib/activity-queries.ts          # modified — real fetchSms/normalizeSms/getSmsDetail
hail-website/app/console/activity/ActivityClient.tsx  # modified — remove "SMS · NOT SHIPPED YET" list stub + kind==="sms" early-return
hail-website/app/console/activity/ActivityDrawer.tsx  # modified — replace "SMS not shipped" DrawerEmpty with real sms detail render (NEW: refactor extracted the drawer)
hail-website/app/console/activity/detail.ts   # modified — sms branch in resolveActivityDetail
hail-website/app/console/layout.tsx           # modified — flip SMS nav placeholder to a real link
hail-website/app/console/channel-tabs.ts      # modified — add SMS_TABS

hail-website/app/console/sms/page.tsx         # new
hail-website/app/console/sms/actions.ts       # new
hail-website/app/console/sms/SenderIdPanel.tsx     # new
hail-website/app/console/sms/NumbersPanel.tsx      # new
hail-website/app/console/sms/SuppressionPanel.tsx  # new

hail-website/app/(marketing)/pricing/page.tsx # modified — tiered rates + monthly fees in copy
```

---

### Task 1 [hail repo]: Embed pricing tier into `usage_events.ref` for SMS

**Files:**

- Create: `core/hailhq/core/pricing_tier.py`
- Modify: `api/hailhq/api/routes/sms.py`
- Test: `core/tests/test_pricing_tier.py`

**Interfaces:**

- Produces: `classify_pricing_tier(to_e164: str) -> Literal["us", "ca", "row"]`.

> **Audit note (updated 2026-07-15):** `core/hailhq/core/sender_id.py` now exists (Numbers & Sender ID / PR #17), but it does **not** contain a `_CANADIAN_AREA_CODES` list — that list was **removed during that phase's review** as dead code (US and Canada both resolve to `always_number` via the `+1` fallthrough, so the NPA distinction wasn't load-bearing for Sender ID). So keep `classify_pricing_tier` fully standalone with its **own** copy of the NPA list below, and **delete the "keep in sync with `sender_id.py`" comment entirely** — there is no list there to sync with, and the two modules are deliberately decoupled anyway (different partitions: billing tiers US/CA/RoW vs. Sender-ID corridors). The `create_sms` handler and its `write_usage_event(..., ref=f"sms:{sms.id}")` call (still 2-part; re-confirm the exact line, it has shifted with the Task-5 sender-resolution changes) are what Step 5 edits.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_pricing_tier.py
"""Tests for SMS pricing-tier classification (US/Canada/rest-of-world).

Distinct from core.hailhq.core.sender_id's corridor classification — that
module answers "can this destination use an alphanumeric Sender ID";
this one answers "which billing rate applies." Same E.164 input, two
different, unrelated output axes.
"""

from __future__ import annotations

from hailhq.core.pricing_tier import classify_pricing_tier


def test_us_number_is_us_tier() -> None:
    assert classify_pricing_tier("+14155551234") == "us"


def test_canadian_area_code_is_ca_tier() -> None:
    assert classify_pricing_tier("+14165551234") == "ca"  # 416 = Toronto


def test_uk_number_is_row_tier() -> None:
    assert classify_pricing_tier("+447911123456") == "row"


def test_germany_number_is_row_tier() -> None:
    assert classify_pricing_tier("+491701234567") == "row"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_pricing_tier.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# core/hailhq/core/pricing_tier.py
"""SMS billing-tier classification: US / Canada / rest-of-world.

Distinct from sender_id.py's corridor classification (which answers a
different question — Sender ID eligibility, not billing rate) even
though both parse an E.164 prefix. Keep these separate; do not merge
them just because the input type overlaps.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["classify_pricing_tier"]

PricingTier = Literal["us", "ca", "row"]

# Canadian NPA list, owned solely by this module. (sender_id.py deliberately
# has NO such list — US and Canada resolve identically there — so there is
# nothing to keep in sync; billing tiers and Sender-ID corridors are separate
# partitions of the world by design.)
_CANADIAN_AREA_CODES = frozenset(
    {"204", "226", "236", "249", "250", "289", "306", "343", "365", "387", "403", "416",
     "418", "431", "437", "438", "450", "506", "514", "519", "548", "579", "581", "587",
     "604", "613", "639", "647", "672", "705", "709", "778", "780", "782", "807", "819",
     "825", "867", "873", "902", "905"}
)


def classify_pricing_tier(to_e164: str) -> PricingTier:
    digits = to_e164.lstrip("+")
    if digits.startswith("1"):
        area_code = digits[1:4]
        return "ca" if area_code in _CANADIAN_AREA_CODES else "us"
    return "row"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_pricing_tier.py -v`
Expected: 4 passed

- [ ] **Step 5: Embed the tier in the usage-event ref**

In `api/hailhq/api/routes/sms.py`, find the `write_usage_event(...)` call in `create_sms` (added in Phase 1: SMS Outbound Core) and change its `ref` argument:

```python
from hailhq.core.pricing_tier import classify_pricing_tier

# ... at the write_usage_event call site:
    tier = classify_pricing_tier(sms.to_e164)
    await write_usage_event(
        organization_id=principal.organization_id,
        channel="sms",
        units=sms.segment_count,
        ref=f"sms:{sms.id}:{tier}",
    )
```

- [ ] **Step 6: Update the existing API test asserting on `ref` shape**

`api/tests/test_sms_api.py` **already asserts the 2-part ref** — confirmed at `api/tests/test_sms_api.py:146`: `stmt = select(UsageEvent).where(UsageEvent.ref == f"sms:{body['id']}")`. Update that query/assertion to expect the 3-part `sms:<uuid>:<tier>` form (the fixture number determines the tier — a `+1` US number → `:us`). Pattern for the assertion:

```python
    from hailhq.core.models import UsageEvent
    row = (
        await async_session.execute(select(UsageEvent).where(UsageEvent.channel == "sms"))
    ).scalar_one()
    assert row.ref == f"sms:{sms_id}:us"  # +14155551234 is a US number
```

- [ ] **Step 7: Run regression suites**

Run: `cd core && uv run pytest -v` and `cd api && uv run pytest -v`
Expected: all passed

- [ ] **Step 8: Commit**

```bash
git add core/hailhq/core/pricing_tier.py core/tests/test_pricing_tier.py api/hailhq/api/routes/sms.py api/tests/test_sms_api.py
git commit -m "feat(core,api): embed sms pricing tier into usage_events ref"
```

---

### Task 2 [hail-website]: Tiered SMS rates + monthly fee constants

**Files:**

- Modify: `lib/private-rates.ts`
- Modify: `lib/__tests__/private-rates.test.ts`

**Interfaces:**

- Modifies: `rateUsageCents` to accept a tier parsed from `ref` for the `sms` channel.
- Produces: `getPublicFacingMonthlyFees(): { dedicatedNumberUsdPerMonth: number; tenDlcComplianceUsdPerMonth: number }`.

- [ ] **Step 1: Write the failing test**

```typescript
// lib/__tests__/private-rates.test.ts — replace the existing "sms pricing" describe block with:
describe("sms pricing", () => {
  it("charges 2.5 cents per segment for US destinations", () => {
    expect(rateUsageCents("sms", 1, { ref: "sms:abc-123:us" })).toBe(2.5);
  });
  it("charges 3.5 cents per segment for Canada", () => {
    expect(rateUsageCents("sms", 1, { ref: "sms:abc-123:ca" })).toBe(3.5);
  });
  it("charges 20 cents flat per segment for rest-of-world", () => {
    expect(rateUsageCents("sms", 1, { ref: "sms:abc-123:row" })).toBe(20);
  });
  it("multiplies by segment count", () => {
    expect(rateUsageCents("sms", 3, { ref: "sms:abc-123:us" })).toBe(7.5);
  });
  it("falls back to the US rate for a legacy 2-part ref with no tier", () => {
    // Rows written before this change (Phase 1) have ref="sms:<uuid>" with
    // no tier segment — treat as US rather than crashing or mispricing.
    expect(rateUsageCents("sms", 1, { ref: "sms:abc-123" })).toBe(2.5);
  });
});

describe("sms monthly fees", () => {
  it("exposes the dedicated-number fee at cost, no margin", () => {
    expect(getPublicFacingMonthlyFees().dedicatedNumberUsdPerMonth).toBeCloseTo(
      1.15,
      2,
    );
  });
  it("exposes the 10DLC compliance fee", () => {
    expect(
      getPublicFacingMonthlyFees().tenDlcComplianceUsdPerMonth,
    ).toBeCloseTo(1.0, 2);
  });
});
```

Note: `rateUsageCents`'s signature is changing (adding a required-for-sms third argument) — this is a breaking change to every existing call site. Check `lib/usage-rater.ts`'s call to `rateUsageCents(channel, units)` (it will need to pass `{ ref: row.ref }` too) and update it in this same task, along with any other call site `grep -rn "rateUsageCents(" --include="*.ts"` finds.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hail-website && npx vitest run lib/__tests__/private-rates.test.ts`
Expected: FAIL — old 1¢ flat rate, no monthly-fees export, signature mismatch.

- [ ] **Step 3: Update `private-rates.ts`**

```typescript
// lib/private-rates.ts
import "server-only";
import type { Rates } from "@/lib/billing-tiers";

/**
 * ... (keep the existing module docstring, extend it) ...
 *
 * SMS is tiered by destination since carrier costs vary sharply by
 * country: US and Canada are priced individually (Canada's wholesale
 * cost runs higher than US), everything else is one flat rest-of-world
 * rate. The tier is embedded in the usage_events.ref the hail API writes
 * (format `sms:<uuid>:<tier>`, tier in "us"|"ca"|"row") since there's no
 * other column carrying it — see hail/core/hailhq/core/pricing_tier.py.
 * Legacy rows written before this change have a 2-part ref with no tier
 * and are treated as "us" (the only tier that existed when they were
 * priced under the old flat-rate placeholder).
 */
const RATES_CENTS_PER_UNIT = {
  voice_cents_per_ms: 9 / 60_000,
  sms_cents_per_segment_us: 2.5,
  sms_cents_per_segment_ca: 3.5,
  sms_cents_per_segment_row: 20.0,
  email_cents_per_message: 0.2,
} as const;

/** Flat monthly fees, billed once per org per billing cycle (not
 * per-unit) — see lib/monthly-fee-rater.ts. Dedicated number is priced
 * at cost (Twilio's own ~$1.15/mo US long-code rate, no margin, since
 * SMS margin comes from per-segment usage and the compliance fee, not
 * from holding a number). The 10DLC fee is itemized separately even
 * though the underlying platform registration is shared across all
 * orgs, not billed per-org at Twilio's actual cost. */
const MONTHLY_FEES_CENTS = {
  dedicated_number_cents_per_month: 115,
  ten_dlc_compliance_cents_per_month: 100,
} as const;

export type UsageChannel = "voice" | "sms" | "email";

type SmsRateContext = { ref: string };

function smsTierFromRef(ref: string): "us" | "ca" | "row" {
  const parts = ref.split(":");
  const tier = parts[2];
  if (tier === "ca" || tier === "row") return tier;
  return "us"; // default + legacy-row fallback
}

export function rateUsageCents(
  channel: UsageChannel,
  units: number,
  context?: SmsRateContext,
): number {
  if (units <= 0) return 0;
  const cents = (() => {
    switch (channel) {
      case "voice":
        return units * RATES_CENTS_PER_UNIT.voice_cents_per_ms;
      case "sms": {
        const tier = context ? smsTierFromRef(context.ref) : "us";
        const perSegment =
          tier === "ca"
            ? RATES_CENTS_PER_UNIT.sms_cents_per_segment_ca
            : tier === "row"
              ? RATES_CENTS_PER_UNIT.sms_cents_per_segment_row
              : RATES_CENTS_PER_UNIT.sms_cents_per_segment_us;
        return units * perSegment;
      }
      case "email":
        return units * RATES_CENTS_PER_UNIT.email_cents_per_message;
      default:
        console.error(`unknown usage channel: ${channel}`);
        return 0;
    }
  })();
  return Math.max(0, Math.ceil(cents * 10) / 10);
}

export function getPublicFacingRates(): Rates {
  return {
    voice_usd_per_minute:
      (RATES_CENTS_PER_UNIT.voice_cents_per_ms * 60_000) / 100,
    sms_usd_per_segment: RATES_CENTS_PER_UNIT.sms_cents_per_segment_us / 100,
    email_usd_per_send: RATES_CENTS_PER_UNIT.email_cents_per_message / 100,
  };
}

/** Public-facing flat monthly fees, kept alongside the per-unit rates for
 * the same single-source-of-truth reason (lib/pricing-format.ts's own
 * comment: "a rate change in private-rates.ts propagates everywhere by
 * construction"). Deliberately a separate accessor from getPublicFacingRates
 * since Rates is a per-unit type consumed by estimateFor/buildTiers, which
 * do per-unit division — a flat fee doesn't fit that shape. */
export function getPublicFacingMonthlyFees() {
  return {
    dedicatedNumberUsdPerMonth:
      MONTHLY_FEES_CENTS.dedicated_number_cents_per_month / 100,
    tenDlcComplianceUsdPerMonth:
      MONTHLY_FEES_CENTS.ten_dlc_compliance_cents_per_month / 100,
  };
}
```

Note `getPublicFacingRates().sms_usd_per_segment` now reports the US tier as the single representative rate for the top-up-tier estimate math (`estimateFor`/`buildTiers` in `billing-tiers.ts` only handle one scalar per channel) — this is a deliberate simplification flagged in the design spec's own research findings, not an oversight; the pricing page's rate panel and calculator will need a small copy note that international rates differ (see Task 7).

- [ ] **Step 4: Update `usage-rater.ts`'s call site**

Find `rateUsageCents(row.channel, row.units)` (or equivalent) in `lib/usage-rater.ts` and change it to `rateUsageCents(row.channel, row.units, { ref: row.ref })` — `row.ref` is already selected by the existing query (confirm the exact column alias used in that file's `SELECT` and match it).

- [ ] **Step 5: Run test to verify it passes**

Run: `cd hail-website && npx vitest run lib/__tests__/private-rates.test.ts`
Expected: all passed

- [ ] **Step 6: Run the full vitest suite for regressions**

Run: `cd hail-website && npx vitest run`
Expected: all passed (check specifically for any other test asserting the old flat 1¢ sms rate or the two-argument `rateUsageCents` signature, e.g. `price-drift.test.ts` if it exists per earlier project history — update it to the new tiered figures too)

- [ ] **Step 7: Commit**

```bash
git add lib/private-rates.ts lib/__tests__/private-rates.test.ts lib/usage-rater.ts
git commit -m "feat(billing): switch sms to tiered pricing, add monthly fee constants"
```

---

### Task 3 [hail-website]: Monthly-fee rater

**Files:**

- Create: `lib/monthly-fee-rater.ts`
- Create: `lib/__tests__/monthly-fee-rater.test.ts`
- Create: `app/api/internal/monthly-fees/rate/route.ts`
- Modify: `app/console/billing/page.tsx`

**Interfaces:**

- Produces: `monthlyFeeIdempotencyKey(orgId: string, feeKind: "dedicated_number" | "ten_dlc_compliance", billingMonth: string): string`; `rateMonthlyFees(): Promise<{ priced: number; skipped: number }>`.

- [ ] **Step 1: Write the failing test (pure-logic parts only)**

Per this repo's existing convention (`lib/usage-rater.ts` itself has no test file — DB-touching raters aren't unit-tested at this layer here), this task tests only the extractable pure logic (the idempotency-key format and which orgs/fees are due), not the live Postgres path:

```typescript
// lib/__tests__/monthly-fee-rater.test.ts
import { describe, expect, it } from "vitest";
import { monthlyFeeIdempotencyKey } from "@/lib/monthly-fee-rater";

describe("monthlyFeeIdempotencyKey", () => {
  it("is stable for the same org/fee/month", () => {
    const a = monthlyFeeIdempotencyKey("org-1", "dedicated_number", "2026-07");
    const b = monthlyFeeIdempotencyKey("org-1", "dedicated_number", "2026-07");
    expect(a).toBe(b);
  });
  it("differs across fee kinds for the same org and month", () => {
    const a = monthlyFeeIdempotencyKey("org-1", "dedicated_number", "2026-07");
    const b = monthlyFeeIdempotencyKey(
      "org-1",
      "ten_dlc_compliance",
      "2026-07",
    );
    expect(a).not.toBe(b);
  });
  it("differs across billing months for the same org and fee", () => {
    const a = monthlyFeeIdempotencyKey("org-1", "dedicated_number", "2026-07");
    const b = monthlyFeeIdempotencyKey("org-1", "dedicated_number", "2026-08");
    expect(a).not.toBe(b);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hail-website && npx vitest run lib/__tests__/monthly-fee-rater.test.ts`
Expected: FAIL with a module-not-found error.

- [ ] **Step 3: Write the implementation**

```typescript
// lib/monthly-fee-rater.ts
import "server-only";
import { pool } from "@/lib/db";
import { getPublicFacingMonthlyFees } from "@/lib/private-rates";

/**
 * Rates the two flat monthly SMS fees (dedicated number, 10DLC
 * compliance) — a parallel mechanism to lib/usage-rater.ts, which only
 * handles per-unit usage_events rows. There's no per-cycle source row to
 * mark "priced" here (a dedicated number's existence is a *state*, not a
 * discrete event), so idempotency is keyed on (org, fee kind, billing
 * month) instead of a source-event id.
 *
 * Invocation follows the same pattern usage-rater.ts already uses in this
 * repo (there is no real cron infrastructure here yet — confirmed, not
 * assumed): an opportunistic call on billing page render, plus this
 * internal HMAC-signed endpoint any future scheduler can hit. An org that
 * never opens its billing page won't get billed promptly — a pre-existing
 * limitation of this repo's billing pattern, not new to this fee.
 */

export function monthlyFeeIdempotencyKey(
  orgId: string,
  feeKind: "dedicated_number" | "ten_dlc_compliance",
  billingMonth: string,
): string {
  return `monthly_fee:${orgId}:${feeKind}:${billingMonth}`;
}

function currentBillingMonth(): string {
  const now = new Date();
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
}

export async function rateMonthlyFees(): Promise<{
  priced: number;
  skipped: number;
}> {
  const billingMonth = currentBillingMonth();
  const fees = getPublicFacingMonthlyFees();
  const client = await pool.connect();
  let priced = 0;
  let skipped = 0;
  try {
    await client.query("BEGIN");

    // Orgs owed the 10DLC compliance fee: any org with at least one
    // dedicated (non-pool) phone number carrying an sms capability.
    const { rows: smsOrgs } = await client.query<{ organization_id: string }>(
      `SELECT DISTINCT organization_id FROM phone_numbers
        WHERE is_pool = FALSE AND 'sms' = ANY(capabilities) AND organization_id IS NOT NULL`,
    );
    for (const { organization_id } of smsOrgs) {
      const ref = monthlyFeeIdempotencyKey(
        organization_id,
        "ten_dlc_compliance",
        billingMonth,
      );
      const { rowCount } = await client.query(
        `INSERT INTO account_credits (organization_id, kind, channel, amount_cents, qty, ref, source, created_at)
         SELECT $1, 'debit', 'sms', $2, 1, $3, 'monthly_fee', now()
         WHERE NOT EXISTS (SELECT 1 FROM account_credits WHERE organization_id = $1 AND ref = $3)`,
        [
          organization_id,
          -Math.round(fees.tenDlcComplianceUsdPerMonth * 100 * 10) / 10,
          ref,
        ],
      );
      if (rowCount) priced++;
      else skipped++;
    }

    // Orgs owed the per-number dedicated-number fee: one debit per
    // dedicated sms-capable number (an org with 2 such numbers pays twice).
    const { rows: numbers } = await client.query<{
      id: string;
      organization_id: string;
    }>(
      `SELECT id, organization_id FROM phone_numbers
        WHERE is_pool = FALSE AND 'sms' = ANY(capabilities) AND organization_id IS NOT NULL`,
    );
    for (const { id, organization_id } of numbers) {
      const ref = monthlyFeeIdempotencyKey(
        `${organization_id}:${id}`,
        "dedicated_number",
        billingMonth,
      );
      const { rowCount } = await client.query(
        `INSERT INTO account_credits (organization_id, kind, channel, amount_cents, qty, ref, source, created_at)
         SELECT $1, 'debit', 'sms', $2, 1, $3, 'monthly_fee', now()
         WHERE NOT EXISTS (SELECT 1 FROM account_credits WHERE organization_id = $1 AND ref = $3)`,
        [
          organization_id,
          -Math.round(fees.dedicatedNumberUsdPerMonth * 100 * 10) / 10,
          ref,
        ],
      );
      if (rowCount) priced++;
      else skipped++;
    }

    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
  return { priced, skipped };
}
```

Note: `amount_cents` is negated (`-Math.round(...)`) since `account_credits`'s own CHECK constraint (confirmed in migration `0001_initial.py` and widened in `0019_...`) requires `kind='debit' AND amount_cents < 0` — mirror the exact sign convention `usage-rater.ts` already uses for its own debit inserts (re-check that file's insert statement for the precise sign/rounding idiom and match it exactly rather than trusting this reconstruction).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hail-website && npx vitest run lib/__tests__/monthly-fee-rater.test.ts`
Expected: 3 passed

- [ ] **Step 5: Add the internal HMAC-signed trigger route**

Mirror `app/api/internal/usage-events/rate/route.ts` exactly (same HMAC-verification-against-`HAIL_INTERNAL_SECRET` pattern, same `timingSafeEqual` check):

```typescript
// app/api/internal/monthly-fees/rate/route.ts
import { NextResponse } from "next/server";
import { rateMonthlyFees } from "@/lib/monthly-fee-rater";
// ... mirror the exact HMAC-verification imports/logic from
// app/api/internal/usage-events/rate/route.ts ...

export async function POST(request: Request) {
  // ... same signature verification as the usage-events rate route ...
  const result = await rateMonthlyFees();
  return NextResponse.json(result);
}
```

Read the real `usage-events/rate/route.ts` file first and copy its exact verification block rather than approximating it here.

- [ ] **Step 6: Wire the opportunistic call into the billing page**

In `app/console/billing/page.tsx`, alongside the existing opportunistic `rateUnpricedUsage()` call, add:

```typescript
import { rateMonthlyFees } from "@/lib/monthly-fee-rater";

// ... alongside the existing `rateUnpricedUsage().catch((err) => console.warn(...))` call:
rateMonthlyFees().catch((err) =>
  console.warn("monthly fee rating failed", err),
);
```

Match the exact existing error-swallowing style at that call site.

- [ ] **Step 7: Run full vitest suite**

Run: `cd hail-website && npx vitest run`
Expected: all passed

- [ ] **Step 8: Commit**

```bash
git add lib/monthly-fee-rater.ts lib/__tests__/monthly-fee-rater.test.ts app/api/internal/monthly-fees/rate/route.ts app/console/billing/page.tsx
git commit -m "feat(billing): add monthly fee rater for dedicated numbers and 10dlc compliance"
```

---

### Task 4 [hail-website]: Wire real SMS data into the unified activity log

**Files:**

- Modify: `lib/activity-queries.ts`
- Modify: `app/console/activity/ActivityClient.tsx`
- Modify: `app/console/activity/ActivityDrawer.tsx` — **audit 2026-07-09: the drawer was extracted into its own file since this plan was written; there are TWO separate sms "not shipped" stubs now, not one.**
- Modify: `app/console/activity/detail.ts`
- Modify: `app/console/layout.tsx`

**Interfaces:**

- Modifies: `getActivity`/`getActivityCount` (remove the hardcoded `channel === "sms"` empty-result branches), adds `fetchSms`/`normalizeSms`/`SmsDetail`/`getSmsDetail` mirroring the existing `fetchCalls`/`normalizeCall`/`CallDetail`/`getCallDetail` shapes exactly.

- [ ] **Step 1: Write the failing test**

Check for an existing `lib/__tests__/activity-queries.test.ts` first — if one exists, append to it; if not, this task's DB-touching queries follow this repo's existing convention (no unit test for the raw-SQL fetchers themselves, per `usage-rater.ts`/`billing-queries.ts` precedent) and this step instead adds a test for whatever pure normalization logic can be isolated (mirroring how `normalizeCall`/`normalizeEmail` may or may not already have direct test coverage — check first, match the existing pattern rather than inventing a new one).

- [ ] **Step 2: Add `fetchSms`/`normalizeSms` to `activity-queries.ts`**

Read the exact `fetchCalls`/`normalizeCall` pair first (lines ~139-171 and ~80-113 per prior research) and write `fetchSms`/`normalizeSms` as a structural mirror, querying the `sms` table (now populated by both outbound sends and inbound ingest from the prior two plans) instead of `calls`. Remove the `if (channel === "sms") return [];` (in `getActivity`) and the `channel === "sms"` → `0` branch (in `getActivityCount`), replacing both with real calls to the new `fetchSms`/count-query, exactly parallel to how the `call`/`email` branches already work.

- [ ] **Step 3: Add `SmsDetail`/`getSmsDetail`**

Mirror `CallDetail`/`getCallDetail` (or `EmailDetail`/`getEmailDetail`, whichever is structurally closer — SMS has a single body + status, closer to `Call`'s shape than `Email`'s multi-event shape) and wire an `sms` branch into `resolveActivityDetail` in `app/console/activity/detail.ts`.

- [ ] **Step 4: Remove BOTH "not shipped" sms placeholders (two files, post-refactor)**

  **4a — `app/console/activity/ActivityClient.tsx`:** remove the `showingSms` conditional (`const showingSms = activeChannel === "sms"` ~line 145; the `showingSms ? (...)` block rendering `★ SMS · NOT SHIPPED YET` ~line 178-180) and the `if (kind === "sms") return; // placeholder modality` early-exit ~line 89 — both should fall through to the same real-data paths `call`/`email` already use.

  **4b — `app/console/activity/ActivityDrawer.tsx`:** the drawer (extracted since this plan was written) renders a second stub at ~line 86-87: `{!pending && detail?.kind === "sms" && (<DrawerEmpty title="SMS not shipped" body="The SMS surface will land in v1.2." />)}`. Replace that with a real sms-detail render block, mirroring the existing `detail?.kind === "call"`/`"email"` render branches in the same file, fed by the `SmsDetail` shape added in Step 3. This is the render side of the `detail.ts` `resolveActivityDetail` sms branch — both must change together or the drawer stays empty.

- [ ] **Step 5: Flip the nav placeholder**

In `app/console/layout.tsx`, replace:

```tsx
<span className="it it-soon">
  <span>SMS</span>
  <span className="soon-tag">soon</span>
</span>
```

with:

```tsx
<Link className="it" href="/console/sms">
  <span>SMS</span>
</Link>
```

- [ ] **Step 6: Manual verification**

Run the dev server (`pnpm dev` or the project's documented command), sign in, send a test SMS via the API (from a prior phase's `POST /sms`), and confirm it appears in `/console/activity` with the `sms` filter tab — this UI change has no automated test at this layer per the repo's existing convention for activity-log rendering (check `app/console/activity/__tests__/` for what IS covered — likely `parse-params.test.ts`/`presign.test.ts`, i.e. pure logic, not the rendered list itself).

- [ ] **Step 7: Commit**

```bash
git add lib/activity-queries.ts app/console/activity/ActivityClient.tsx app/console/activity/ActivityDrawer.tsx app/console/activity/detail.ts app/console/layout.tsx
git commit -m "feat(console): wire real sms data into the unified activity log"
```

---

### Task 5 [hail-website]: `/console/sms` settings page (Sender ID, Numbers, Suppression)

> **✅ UNBLOCKED as of 2026-07-15 (was BLOCKED 2026-07-09).** The endpoints this task's server actions proxy to now exist via the Numbers & Sender ID backend (**PR #17 — green / `MERGEABLE`**): `PATCH /sms/sender-id`, `POST /numbers`, `POST /numbers/{id}/enable-sms`, `DELETE /sms/suppressions/{number}`, plus the `sms_sender_identities` table `getSenderId` reads and the `messaging_service_sid` column `getNumbers` selects. **Confirm PR #17 is merged to `main` before starting** (grep `api/hailhq/api/routes/{sms,numbers}.py` for the routes, or check `cd api && uv run alembic heads` == `0033`); until it merges, the mutating actions 404. Read-only `getNumbers`/`getSuppressions` already work against the pre-existing `phone_numbers`/`suppressions` tables.

**Files:**

- Create: `lib/sms-queries.ts`
- Create: `app/console/sms/page.tsx`
- Create: `app/console/sms/actions.ts`
- Create: `app/console/sms/SenderIdPanel.tsx`
- Create: `app/console/sms/NumbersPanel.tsx`
- Create: `app/console/sms/SuppressionPanel.tsx`
- Modify: `app/console/channel-tabs.ts`

**Interfaces:**

- Produces: `getSenderId(orgId)`, `getNumbers(orgId)`, `getSuppressions(orgId)` (direct `pool.query` reads, mirroring `lib/custom-domain-queries.ts`'s shape); `getSenderIdAction`/`setSenderIdAction`/`deleteSuppressionAction` (server actions proxying to the hail API via `callHailApiAsOrg`, mirroring `app/console/email/domains/actions.ts`'s `orgApiAction` helper).

- [ ] **Step 1: Write `lib/sms-queries.ts`**

Mirror `lib/custom-domain-queries.ts`'s exact shape (`import "server-only"`, `pool.query` with a typed row shape, camelCase mapping):

```typescript
// lib/sms-queries.ts
import "server-only";
import { pool } from "@/lib/db";

export type SmsSenderIdentity = {
  customSenderId: string | null;
  effectiveDefault: string;
};

export type PhoneNumberRow = {
  id: string;
  e164: string;
  countryCode: string;
  capabilities: string[];
  messagingServiceSid: string | null;
};

export type SuppressionRow = {
  id: string;
  recipient: string;
  reason: string;
  source: string;
  createdAt: string;
};

export async function getSenderId(
  orgId: string | null,
): Promise<SmsSenderIdentity> {
  if (!orgId) return { customSenderId: null, effectiveDefault: "HAIL" };
  const { rows } = await pool.query<{ custom_sender_id: string }>(
    `SELECT custom_sender_id FROM sms_sender_identities WHERE organization_id = $1`,
    [orgId],
  );
  return {
    customSenderId: rows[0]?.custom_sender_id ?? null,
    effectiveDefault: "HAIL",
  };
}

export async function getNumbers(
  orgId: string | null,
): Promise<PhoneNumberRow[]> {
  if (!orgId) return [];
  const { rows } = await pool.query<{
    id: string;
    e164: string;
    country_code: string;
    capabilities: string[];
    messaging_service_sid: string | null;
  }>(
    `SELECT id, e164, country_code, capabilities, messaging_service_sid
       FROM phone_numbers
      WHERE organization_id = $1 AND is_pool = FALSE
      ORDER BY created_at ASC`,
    [orgId],
  );
  return rows.map((r) => ({
    id: r.id,
    e164: r.e164,
    countryCode: r.country_code,
    capabilities: r.capabilities,
    messagingServiceSid: r.messaging_service_sid,
  }));
}

export async function getSuppressions(
  orgId: string | null,
): Promise<SuppressionRow[]> {
  if (!orgId) return [];
  const { rows } = await pool.query<{
    id: string;
    recipient: string;
    reason: string;
    source: string;
    created_at: string;
  }>(
    `SELECT id, recipient, reason, source, created_at
       FROM suppressions
      WHERE organization_id = $1 AND channel = 'sms'
      ORDER BY created_at DESC`,
    [orgId],
  );
  return rows.map((r) => ({
    id: r.id,
    recipient: r.recipient,
    reason: r.reason,
    source: r.source,
    createdAt: r.created_at,
  }));
}
```

- [ ] **Step 2: Write `app/console/sms/actions.ts`**

Mirror `app/console/email/domains/actions.ts`'s `orgApiAction` helper and its `NEXT`/`revalidate()` pattern exactly:

```typescript
// app/console/sms/actions.ts
"use server";

import { revalidatePath } from "next/cache";
import { requireOrgAdmin } from "@/lib/require-org-admin";
import { callHailApiAsOrg } from "@/lib/hail-api";

const NEXT = "/console/sms";

function revalidate() {
  revalidatePath(NEXT);
  revalidatePath("/console");
}

async function orgApiAction<T>(
  path: string,
  init: RequestInit,
  opts?: { skipRevalidate?: boolean },
) {
  await requireOrgAdmin(NEXT);
  try {
    const body = await callHailApiAsOrg<T>(path, init);
    if (!opts?.skipRevalidate) revalidate();
    return { ok: true as const, ...body };
  } catch (err) {
    return { ok: false as const, error: String(err) };
  }
}

export async function setSenderIdAction(customSenderId: string | null) {
  return orgApiAction("/sms/sender-id", {
    method: "PATCH",
    body: JSON.stringify({ custom_sender_id: customSenderId }),
  });
}

export async function acquireNumberAction(countryCode: string) {
  return orgApiAction("/numbers", {
    method: "POST",
    body: JSON.stringify({ country_code: countryCode, number_type: "local" }),
  });
}

export async function enableSmsOnNumberAction(numberId: string) {
  return orgApiAction(`/numbers/${numberId}/enable-sms`, { method: "POST" });
}

export async function deleteSuppressionAction(number: string) {
  return orgApiAction(`/sms/suppressions/${encodeURIComponent(number)}`, {
    method: "DELETE",
  });
}
```

(Read `orgApiAction`'s real body in `email/domains/actions.ts` before finalizing this — the reconstruction above is close but verify the exact error-shape/`callHailApiAsOrg` generic signature against the real file.)

- [ ] **Step 3: Write the panel components**

Follow `WebhooksClient.tsx`'s table+row-actions template for `NumbersPanel`/`SuppressionPanel` (list-shaped, with a per-row destructive action gated by `confirm(...)`), and a simpler single-value-form template (matching `EmailIdentityPanel.tsx`'s general shape — props = current value + `canManage`, local state, save button, `useTransition`) for `SenderIdPanel`:

```tsx
// app/console/sms/SenderIdPanel.tsx
"use client";

import { useState, useTransition } from "react";
import { setSenderIdAction } from "./actions";

export function SenderIdPanel({
  customSenderId,
  effectiveDefault,
  canManage,
}: {
  customSenderId: string | null;
  effectiveDefault: string;
  canManage: boolean;
}) {
  const [value, setValue] = useState(customSenderId ?? "");
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  const onSave = () => {
    setError(null);
    startTransition(async () => {
      const result = await setSenderIdAction(
        value.trim() === "" ? null : value.trim(),
      );
      if (!result.ok) setError(result.error);
    });
  };

  return (
    <div className="c-panel">
      <div className="ph2">
        <h2>Sender ID</h2>
      </div>
      <p>
        Used only for outbound texts to countries that do not require
        pre-registration (e.g. Germany, UK). US, Canada, and
        registration-required countries always use your dedicated number or the
        platform default (&quot;{effectiveDefault}&quot;) instead.
      </p>
      {canManage && (
        <>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            maxLength={11}
            placeholder={effectiveDefault}
            disabled={pending}
          />
          <button onClick={onSave} disabled={pending}>
            {pending ? "Saving…" : "Save"}
          </button>
          {error && <p className="error">{error}</p>}
        </>
      )}
    </div>
  );
}
```

`NumbersPanel.tsx`/`SuppressionPanel.tsx` follow `WebhooksClient.tsx`'s row-list-with-actions structure — read that file's full content before writing these two, and match its `confirm(...)`/`useTransition`/`router.refresh()` conventions exactly rather than inventing a new interaction pattern.

- [ ] **Step 4: Write `app/console/sms/page.tsx`**

Mirror `app/console/email/domains/page.tsx`'s exact shape (`requireSession` → resolve `orgId` → `Promise.all` fetch every panel's data + role → header block → panels):

```tsx
// app/console/sms/page.tsx
import { getActiveOrgIdForSession, requireSession } from "@/lib/auth";
import { getSenderId, getNumbers, getSuppressions } from "@/lib/sms-queries";
import { getOrgMemberRole } from "@/lib/require-org-admin";
import { ChannelTabs } from "@/app/console/ChannelTabs";
import { SMS_TABS } from "@/app/console/channel-tabs";
import { SenderIdPanel } from "./SenderIdPanel";
import { NumbersPanel } from "./NumbersPanel";
import { SuppressionPanel } from "./SuppressionPanel";

export const dynamic = "force-dynamic";

export default async function SmsPage() {
  const session = await requireSession("/signin?next=/console/sms");
  const orgId = await getActiveOrgIdForSession(session);

  const [senderId, numbers, suppressions, role] = await Promise.all([
    getSenderId(orgId),
    getNumbers(orgId),
    getSuppressions(orgId),
    orgId ? getOrgMemberRole(orgId, session.user.id) : Promise.resolve(null),
  ]);
  const canManage = role === "owner" || role === "admin";

  return (
    <section className="console-pane">
      <ChannelTabs tabs={SMS_TABS} />
      <div className="c-ph">
        <div>
          <h1>
            SMS <em>— sender identity &amp; numbers.</em>
          </h1>
          <p className="lede">
            Your Sender ID, dedicated numbers, and opted-out recipients.
            Workspace controls require <b>admin</b> or <b>owner</b> role.
          </p>
        </div>
      </div>
      <SenderIdPanel
        customSenderId={senderId.customSenderId}
        effectiveDefault={senderId.effectiveDefault}
        canManage={canManage}
      />
      <NumbersPanel numbers={numbers} canManage={canManage} />
      <SuppressionPanel suppressions={suppressions} canManage={canManage} />
    </section>
  );
}
```

- [ ] **Step 5: Add `SMS_TABS` to `channel-tabs.ts`**

Mirror `EMAIL_TABS` exactly — read the real file first (referenced as `app/console/channel-tabs.ts`, full 22 lines) and add:

```typescript
export const SMS_TABS: ChannelTab[] = [
  { label: "Activity", href: "/console/activity?channel=sms" },
  { label: "Settings", href: "/console/sms" },
];
```

(Confirm the real `ChannelTab` type's exact field names against `EMAIL_TABS`'s definition before finalizing — this is a structural guess pending that read.)

- [ ] **Step 6: Manual verification**

Run the dev server, sign in as an org admin, navigate to `/console/sms`, set a custom Sender ID, acquire a number (if the Numbers & Sender ID backend phase is live), and confirm a suppressed number appears after a test STOP webhook (from the Inbound & Compliance phase) fires.

- [ ] **Step 7: Commit**

```bash
git add lib/sms-queries.ts app/console/sms/ app/console/channel-tabs.ts
git commit -m "feat(console): add /console/sms settings page (sender id, numbers, suppressions)"
```

---

### Task 6 [hail-website]: Pricing page update

**Files:**

- Modify: `app/(marketing)/pricing/page.tsx`

**Interfaces:**

- Consumes: `getPublicFacingRates()`, `getPublicFacingMonthlyFees()` (Task 2).

- [ ] **Step 1: Update the rate panel and FAQ copy**

In `app/(marketing)/pricing/page.tsx`, the existing SMS rate panel (`fmtCentsRate(rates.sms_usd_per_segment)`, "Pictures included. Carrier fees passed straight through, never marked up.") needs two changes: (1) the blurb text is stale — MMS/pictures aren't included per the design spec (text-only for v1) and carrier fees are not literally "passed through never marked up" anymore under tiered pricing — replace with accurate copy; (2) add the monthly fees somewhere near the SMS panel or in the FAQ, using `getPublicFacingMonthlyFees()`.

```typescript
import { getPublicFacingMonthlyFees } from "@/lib/private-rates";

// ... inside PricingPage():
const monthlyFees = getPublicFacingMonthlyFees();

const ratePanels = [
  // ... voice panel unchanged ...
  {
    big: fmtCentsRate(rates.sms_usd_per_segment),
    unit: "Per text (US)",
    blurb: `Canada and the rest of the world are priced separately — see the FAQ. $${monthlyFees.dedicatedNumberUsdPerMonth.toFixed(2)}/mo for a dedicated number, $${monthlyFees.tenDlcComplianceUsdPerMonth.toFixed(2)}/mo carrier compliance fee.`,
  },
  // ... email panel unchanged ...
];
```

Update the FAQ entry "What counts as one text?" and "What about the number and email domain?" to reflect: text-only (no MMS yet), tiered international pricing, and that dedicated numbers now carry the two new monthly fees. Remove the now-stale "Numbers are pooled today, with dedicated numbers you can purchase coming soon" line if the Numbers & Sender ID phase has shipped self-serve acquisition by the time this task lands — otherwise leave it, since falsely claiming self-serve numbers ship before the backend does would be a real accuracy regression.

- [ ] **Step 2: Manual verification**

Run the dev server, visit `/pricing`, confirm the SMS panel and FAQ read correctly and the numbers match `lib/private-rates.ts` exactly (per `lib/pricing-format.ts`'s own invariant that this page renders ONLY through the formatters + `getPublicFacingRates()`/`getPublicFacingMonthlyFees()`, never a hardcoded literal).

- [ ] **Step 3: Run the pricing-calculator test for regressions**

Run: `cd hail-website && npx vitest run app/\(marketing\)/pricing/__tests__/pricing-calculator.test.ts`
Expected: passes, or gets updated if it hardcodes the old flat SMS rate anywhere.

- [ ] **Step 4: Commit**

```bash
git add "app/(marketing)/pricing/page.tsx" "app/(marketing)/pricing/__tests__/pricing-calculator.test.ts"
git commit -m "docs(pricing): update sms copy for tiered rates and monthly fees"
```

---

## Self-Review Notes

- **Spec coverage**: covers the design spec's "Console UI" section in full (activity log, three settings panels, pricing page) plus the "Billing mechanism change" paragraph (new recurring-fee rater). Does not cover docs/changelog/legal (separate plan) — and per research, the legal-doc flip is already substantially done by a parallel workstream, not this plan's job.
- **Placeholder scan**: the plan flags several "verify against the real file before finalizing" notes (e.g. `orgApiAction`'s exact signature, `ChannelTab`'s field names, the account-credits insert's sign convention) — these are explicit verification steps against files whose exact current content wasn't fully captured in research, not unfinished work.
- **Cross-plan consistency**: `pricing_tier.py` (this plan) and `sender_id.py` (Numbers & Sender ID plan) are explicitly kept separate despite overlapping input types — verify this distinction is preserved at implementation time (a future maintainer merging them would silently break either billing or Sender ID eligibility, since US/Canada/RoW billing tiers and US/Canada/UK/Germany/Australia/India Sender-ID-eligibility corridors are not the same partition of countries).

## Remaining Phases (not this plan)

1. **Docs & release** — `docs/setup/sms.md`, CHANGELOG, README milestones. Legal docs (`content/legal/{aup,terms,privacy,dpa}.md`) were **updated 2026-07-09** to cover SMS retention and drop stale "planned" language — confirmed current, do NOT re-edit them for this plan. (Task 6's pricing-page copy is the only marketing/legal-adjacent touchpoint that remains this plan's job.)
