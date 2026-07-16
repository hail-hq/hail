# SMS Billing — Plan 1: Telephony Costs Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a public, schema-validated, staleness-checked `telephony` category to the `costs/` surface so the SMS monthly-fee rater (Plan 2) can price dedicated numbers at cost from a version-controlled source, and customers can verify "no markup" at `hail.so/costs`.

**Architecture:** `costs/` is a set of JSON data files (`llm.json`, `stt.json`, `tts.json`) sharing a `{ version, license, models[] }` envelope, each validated against a JSON Schema on PR and rendered by the `web/` Next.js app. This plan adds `costs/telephony.json` — deliberately a **two-array** file (`numbers[]` + `a2p_10dlc[]`), which does not fit the single-`models[]` envelope. That drives three infra generalizations: the staleness script, the per-category validation, and the `web/` type/render wiring.

**Tech Stack:** JSON Schema (Draft 2020-12, validated via `check-jsonschema`), Node ESM scripts tested with `node:test`, Next.js 16 / React 19 / `@tanstack/react-table` for `web/`.

## Global Constraints

- Repo: `/Users/r/playground/hail/.claude/worktrees/sms-console-ui`, branch `feat/sms-console-ui`. All work is in the **hail** repo (the public one that owns `costs/` and `web/`).
- Data-file envelope version is `2`; `license` is the const `"CC-BY-4.0"`. Copy these verbatim.
- Prices are **decimal strings** (e.g. `"1.15"`), never numbers — matches the schema `decimal` pattern `^(0|[1-9][0-9]*)(\.[0-9]{1,8})?$`.
- `verified_by` must match the GitHub-handle regex `^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$`. Use `r13i` (the handle already used across `costs/*.json`).
- Seed values come from the spec's Research appendix (`docs/superpowers/specs/2026-07-15-sms-billing-model-correction-design.md`). Every row carries `source_url` + `last_verified: "2026-07-15"`.
- `web/` has **no test harness** — do not add one. Data/script layers are TDD'd; `web/` components follow the existing untested `stt-section.tsx` pattern.
- **Out of scope for this plan** (additive, non-blocking): the `web/app/(dispatch)/compare/` page and the `web/app/costs.md` markdown export. Telephony need not appear there for "at cost, clearly stated." Note this in the README so a later PR can add them.
- Commit after each task. Conventional Commits. No `Co-Authored-By` trailer.

---

### Task 1: Telephony schema + seed data file

**Files:**
- Create: `costs/schema/telephony.schema.json`
- Create: `costs/telephony.json`
- Test: manual `check-jsonschema` run (no unit-test framework for schemas; validation is the test)

**Interfaces:**
- Produces: `costs/telephony.json` with shape `{ version: 2, license: "CC-BY-4.0", numbers: NumberRow[], a2p_10dlc: FeeRow[] }`.
  - `NumberRow`: `{ country_code, number_type, display_name, usd_per_month, last_verified, last_changed_at, verification_method, verified_by, source_url, notes? }` where `country_code` is ISO-2 (`"US"`), `number_type` ∈ `local|mobile|toll_free`, `usd_per_month` is a decimal string.
  - `FeeRow`: `{ carrier, fee_kind, direction, usd_per_message?, usd_per_month?, last_verified, last_changed_at, verification_method, verified_by, source_url, notes? }` where `direction` ∈ `outbound|inbound|na`.
  - Consumed by Plan 2's `lib/telephony-costs.ts` (`numbers[]`) and rendered by Task 5's `TelephonySection`.

- [ ] **Step 1: Write the schema**

Create `costs/schema/telephony.schema.json` (modeled on `costs/schema/stt.schema.json`, which uses `$schema` Draft 2020-12, a `decimal` `$def`, `additionalProperties: false`, and a GitHub-handle `verified_by` pattern):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://hail.so/costs/schema/telephony.json",
  "title": "Telephony Costs",
  "description": "Provider COGS for telephony: dedicated phone-number monthly prices and A2P 10DLC carrier/registration fees. Number prices are at-cost pass-through; the rater bills these directly.",
  "type": "object",
  "required": ["version", "license", "numbers", "a2p_10dlc"],
  "additionalProperties": false,
  "properties": {
    "version": { "const": 2 },
    "license": { "const": "CC-BY-4.0" },
    "numbers": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/number" } },
    "a2p_10dlc": { "type": "array", "minItems": 1, "items": { "$ref": "#/$defs/fee" } }
  },
  "$defs": {
    "decimal": { "type": "string", "pattern": "^(0|[1-9][0-9]*)(\\.[0-9]{1,8})?$" },
    "date": { "type": "string", "format": "date" },
    "provenance": {
      "type": "object",
      "required": ["last_verified", "last_changed_at", "verification_method", "verified_by", "source_url"],
      "properties": {
        "last_verified": { "$ref": "#/$defs/date" },
        "last_changed_at": { "$ref": "#/$defs/date" },
        "verification_method": { "enum": ["manual-confirmed", "community-pr"] },
        "verified_by": { "type": "string", "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$" },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    },
    "number": {
      "type": "object",
      "additionalProperties": false,
      "required": ["country_code", "number_type", "display_name", "usd_per_month", "last_verified", "last_changed_at", "verification_method", "verified_by", "source_url"],
      "properties": {
        "country_code": { "type": "string", "pattern": "^[A-Z]{2}$" },
        "number_type": { "enum": ["local", "mobile", "toll_free"] },
        "display_name": { "type": "string", "minLength": 1 },
        "usd_per_month": { "$ref": "#/$defs/decimal" },
        "last_verified": { "$ref": "#/$defs/date" },
        "last_changed_at": { "$ref": "#/$defs/date" },
        "verification_method": { "enum": ["manual-confirmed", "community-pr"] },
        "verified_by": { "type": "string", "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$" },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    },
    "fee": {
      "type": "object",
      "additionalProperties": false,
      "required": ["carrier", "fee_kind", "direction", "last_verified", "last_changed_at", "verification_method", "verified_by", "source_url"],
      "properties": {
        "carrier": { "type": "string", "minLength": 1 },
        "fee_kind": { "enum": ["carrier_passthrough", "tcr_registration", "tcr_campaign_monthly"] },
        "direction": { "enum": ["outbound", "inbound", "na"] },
        "usd_per_message": { "$ref": "#/$defs/decimal" },
        "usd_per_month": { "$ref": "#/$defs/decimal" },
        "usd_one_time": { "$ref": "#/$defs/decimal" },
        "last_verified": { "$ref": "#/$defs/date" },
        "last_changed_at": { "$ref": "#/$defs/date" },
        "verification_method": { "enum": ["manual-confirmed", "community-pr"] },
        "verified_by": { "type": "string", "pattern": "^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$" },
        "source_url": { "type": "string", "format": "uri" },
        "notes": { "type": "string" }
      }
    }
  }
}
```

- [ ] **Step 2: Write the seed data file**

Create `costs/telephony.json`. `numbers[]` seeds only US rows (the only ones Plan 2 needs to bill today; more countries are added as they're verified). `a2p_10dlc[]` carries the carrier + TCR figures from the Research appendix. `usd_per_month` for the US dedicated long code is Twilio's `1.15`.

```json
{
  "version": 2,
  "license": "CC-BY-4.0",
  "numbers": [
    {
      "country_code": "US",
      "number_type": "local",
      "display_name": "US local (10DLC long code)",
      "usd_per_month": "1.15",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us",
      "notes": "At-cost pass-through of Twilio's US long-code monthly rental. No markup."
    },
    {
      "country_code": "US",
      "number_type": "toll_free",
      "display_name": "US toll-free",
      "usd_per_month": "2.15",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us",
      "notes": "At-cost pass-through of Twilio's US toll-free monthly rental."
    }
  ],
  "a2p_10dlc": [
    {
      "carrier": "AT&T",
      "fee_kind": "carrier_passthrough",
      "direction": "outbound",
      "usd_per_message": "0.0035",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us"
    },
    {
      "carrier": "T-Mobile",
      "fee_kind": "carrier_passthrough",
      "direction": "outbound",
      "usd_per_message": "0.0045",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us"
    },
    {
      "carrier": "Verizon",
      "fee_kind": "carrier_passthrough",
      "direction": "outbound",
      "usd_per_message": "0.0045",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us"
    },
    {
      "carrier": "Verizon",
      "fee_kind": "carrier_passthrough",
      "direction": "inbound",
      "usd_per_message": "0.007",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://www.twilio.com/en-us/sms/pricing/us"
    },
    {
      "carrier": "TCR",
      "fee_kind": "tcr_campaign_monthly",
      "direction": "na",
      "usd_per_month": "10.0",
      "last_verified": "2026-07-15",
      "last_changed_at": "2026-07-15",
      "verification_method": "manual-confirmed",
      "verified_by": "r13i",
      "source_url": "https://support.telnyx.com/en/articles/5634625-10dlc-fees-and-charges",
      "notes": "Standard-volume shared campaign, platform-wide total (not per-org). Amortizes to ~0/segment; absorbed into the US per-segment rate, not billed as a line item."
    }
  ]
}
```

- [ ] **Step 3: Validate — this is the failing check**

Run: `pipx run check-jsonschema==0.29.4 --schemafile costs/schema/telephony.schema.json costs/telephony.json`
Expected: PASS (`ok -- validation done`). If it fails, fix the data/schema until it passes.

- [ ] **Step 4: Verify the schema actually rejects bad data**

Temporarily add a row to `numbers[]` with `"usd_per_month": 1.15` (a number, not a string) and re-run the command.
Expected: FAIL (`1.15 is not of type 'string'`). Then revert the bad edit and confirm PASS again. This proves the decimal-string guard works.

- [ ] **Step 5: Commit**

```bash
git add costs/schema/telephony.schema.json costs/telephony.json
git commit -m "feat(costs): add telephony category schema and seed data"
```

---

### Task 2: Wire per-category validation (root script + CI)

**Files:**
- Modify: `package.json` (root, the `costs:validate` script)
- Modify: `.github/workflows/costs-validate.yml:24-29` (add a validate step) and `:32` (the cross-field loop)

**Interfaces:**
- Consumes: `costs/telephony.json` + `costs/schema/telephony.schema.json` from Task 1.
- Produces: `telephony.json` is validated on every PR that touches `costs/**`, and by `pnpm costs:validate` locally.

- [ ] **Step 1: Extend the root validate script**

In the root `package.json`, the `costs:validate` script currently chains three `check-jsonschema` calls (llm, stt, tts). Append a fourth for telephony. Change:

```
"costs:validate": "pipx run check-jsonschema --schemafile costs/schema/llm.schema.json costs/llm.json && pipx run check-jsonschema --schemafile costs/schema/stt.schema.json costs/stt.json && pipx run check-jsonschema --schemafile costs/schema/tts.schema.json costs/tts.json"
```
to add, at the end (same `&&` chain, same `pipx run` prefix used by the existing calls):
```
 && pipx run check-jsonschema --schemafile costs/schema/telephony.schema.json costs/telephony.json
```

- [ ] **Step 2: Run it to confirm all four validate**

Run: `pnpm costs:validate`
Expected: four `ok -- validation done` lines, exit 0.

- [ ] **Step 3: Add the CI validate step**

In `.github/workflows/costs-validate.yml`, after the `Validate TTS data` step (line 29), add:

```yaml
      - name: Validate Telephony data
        run: check-jsonschema --schemafile costs/schema/telephony.schema.json costs/telephony.json
```

- [ ] **Step 4: Extend the cross-field loop**

In the same file, the `Cross-field validity checks` step iterates `for f in costs/llm.json costs/stt.json costs/tts.json; do`. That loop's `jq` reads `.models[]` (aliases + `replaced_by_model_id`), which telephony does not have. **Do not add telephony to this loop** — it has no `models`/`aliases`/`replaced_by_model_id`, so the `jq` would error on a missing `.models`. Leave the loop as-is. Add a comment above the loop to record why telephony is excluded:

```yaml
      # Cross-field checks below are model-shaped (aliases, replaced_by_model_id)
      # and apply only to llm/stt/tts. telephony.json has no models[] and is
      # covered by its own schema validation step above.
      - name: Cross-field validity checks (aliases unique, replaced_by_model_id resolves)
```

- [ ] **Step 5: Commit**

```bash
git add package.json .github/workflows/costs-validate.yml
git commit -m "ci(costs): validate telephony.json on PR and via costs:validate"
```

---

### Task 3: Generalize the staleness script for multi-array files

**Files:**
- Modify: `scripts/costs/check-stale.mjs`
- Test: `scripts/costs/check-stale.test.mjs`

**Interfaces:**
- Consumes: nothing new (reads `costs/*.json` off disk).
- Produces: `check-stale.mjs` staleness-checks `telephony.json`'s `numbers[]` and `a2p_10dlc[]` rows, and treats an unknown data file as a **hard error**, not a silent skip.
- The current script (`check-stale.mjs:36-42`) derives `category` from the filename and hardcodes `data?.models`. This task replaces the hardcoded `.models` with a `ROW_ARRAYS` map keyed by category, each mapping to a list of array keys.

- [ ] **Step 1: Write failing tests**

`scripts/costs/check-stale.test.mjs` uses `node:test` + `node:assert/strict` and tests the pure `findStale(rows, maxAgeDays, now)`. Add tests for a new pure helper `rowsForFile(filename, data)` that this task introduces (returns the flat list of staleable rows across all of a file's arrays, or throws on an unknown file). Append to the existing test file:

```javascript
import { rowsForFile } from './check-stale.mjs';

test('rowsForFile flattens a model-shaped file', () => {
  const rows = rowsForFile('llm.json', { models: [{ model_id: 'a', last_verified: '2026-01-01' }] });
  assert.equal(rows.length, 1);
  assert.equal(rows[0].model_id, 'a');
});

test('rowsForFile flattens telephony across both arrays', () => {
  const rows = rowsForFile('telephony.json', {
    numbers: [{ display_name: 'US local', last_verified: '2026-01-01' }],
    a2p_10dlc: [{ carrier: 'AT&T', last_verified: '2026-01-02' }],
  });
  assert.equal(rows.length, 2);
});

test('rowsForFile throws on an unknown data file', () => {
  assert.throws(() => rowsForFile('mystery.json', { foo: [] }), /unknown costs file/);
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `node --test scripts/costs/`
Expected: FAIL — `rowsForFile` is not exported.

- [ ] **Step 3: Implement `rowsForFile` and route `main()` through it**

In `scripts/costs/check-stale.mjs`, add the map + helper near the top (after the constants, before `findStale`):

```javascript
// Which array(s) hold staleable rows in each costs file. An unrecognized
// file is a hard error, not a silent skip: silently skipping is exactly how a
// price file could rot unnoticed while the invoice keeps citing it.
const ROW_ARRAYS = {
  llm: ['models'],
  stt: ['models'],
  tts: ['models'],
  telephony: ['numbers', 'a2p_10dlc'],
};

export function rowsForFile(filename, data) {
  const category = filename.replace(/\.json$/, '');
  const keys = ROW_ARRAYS[category];
  if (!keys) {
    throw new Error(`unknown costs file: ${filename} (add it to ROW_ARRAYS)`);
  }
  const rows = [];
  for (const key of keys) {
    const arr = data?.[key];
    if (!Array.isArray(arr)) {
      throw new Error(`${filename}: expected array at \`${key}\``);
    }
    for (const row of arr) rows.push({ category, ...row });
  }
  return rows;
}
```

Then change the per-file block in `main()` (currently lines 35-42) from the hardcoded `.models` guard to:

```javascript
    files.map(async (file) => {
      const data = JSON.parse(await readFile(join(DATA_DIR, file), 'utf-8'));
      const rows = rowsForFile(file, data);
      return findStale(rows, maxAge).map((row) => ({ category: row.category, ...row }));
    }),
```

Note: `rowsForFile` already stamps `category`, so the `.map((row) => ({ category, ...row }))` that previously added it is redundant but harmless; simplify it to `findStale(rows, maxAge)` and let the printed rows keep the `category` they already carry. The printout at line 57 reads `row.provider / row.model_id` — telephony rows have neither, so make the label fall back:

```javascript
    const label = row.model_id || row.display_name || row.carrier || '(row)';
    console.log(`- [${row.category}] ${row.provider ?? ''} ${label} — last verified ${row.last_verified} (${days} days ago)`.replace(/\s+/g, ' '));
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test scripts/costs/`
Expected: PASS — all existing `findStale` tests plus the three new `rowsForFile` tests.

- [ ] **Step 5: Run the script end to end against real data**

Run: `pnpm costs:stale` (or `node scripts/costs/check-stale.mjs --max-age 30`)
Expected: exit 0 with `No stale rows` (telephony rows are dated 2026-07-15, within 30 days of the run — if the run is >30 days later they'll correctly show as stale). Confirms telephony is now included and no file errors.

- [ ] **Step 6: Commit**

```bash
git add scripts/costs/check-stale.mjs scripts/costs/check-stale.test.mjs
git commit -m "feat(costs): staleness-check telephony arrays, error on unknown files"
```

---

### Task 4: `web/` types and costs exports for telephony

**Files:**
- Modify: `web/lib/types.ts` (add `TelephonyNumberRow`, `TelephonyFeeRow`)
- Modify: `web/lib/costs.ts` (import + export `telephony`, `telephonySchema`)

**Interfaces:**
- Consumes: `costs/telephony.json`, `costs/schema/telephony.schema.json`.
- Produces: `telephony: CostsFile<...>` and `telephonySchema` exports for Task 5/6/7. `CostsFile<Row>` is the existing generic `{ version, license, models: Row[] }` — but telephony has `numbers`/`a2p_10dlc`, not `models`. So telephony gets its **own** file type rather than reusing `CostsFile`.

- [ ] **Step 1: Add row + file types**

In `web/lib/types.ts`, after the `STTRow` type, add:

```typescript
export interface TelephonyNumberRow {
  country_code: string;
  number_type: 'local' | 'mobile' | 'toll_free';
  display_name: string;
  usd_per_month: string;
  last_verified: string;
  last_changed_at: string;
  verification_method: 'manual-confirmed' | 'community-pr';
  verified_by: string;
  source_url: string;
  notes?: string;
}

export interface TelephonyFeeRow {
  carrier: string;
  fee_kind: 'carrier_passthrough' | 'tcr_registration' | 'tcr_campaign_monthly';
  direction: 'outbound' | 'inbound' | 'na';
  usd_per_message?: string;
  usd_per_month?: string;
  usd_one_time?: string;
  last_verified: string;
  last_changed_at: string;
  verification_method: 'manual-confirmed' | 'community-pr';
  verified_by: string;
  source_url: string;
  notes?: string;
}

export interface TelephonyFile {
  version: number;
  license: string;
  numbers: TelephonyNumberRow[];
  a2p_10dlc: TelephonyFeeRow[];
}
```

- [ ] **Step 2: Export from `web/lib/costs.ts`**

Add to `web/lib/costs.ts` (mirroring the existing `stt` lines):

```typescript
import telephonyJson from '../../costs/telephony.json';
import telephonySchemaJson from '../../costs/schema/telephony.schema.json';
import type { CostsFile, LLMRow, STTRow, TTSRow, TelephonyFile } from './types';
```
(extend the existing `import type` line to add `TelephonyFile`), and at the bottom:
```typescript
export const telephony = telephonyJson as TelephonyFile;
export const telephonySchema = telephonySchemaJson;
```

- [ ] **Step 3: Typecheck via build**

Run: `pnpm site:build`
Expected: build succeeds (there is no standalone `typecheck` script; `next build` runs `tsc`). If it fails on the JSON import, confirm `web/tsconfig.json` has `resolveJsonModule` (it already resolves the other three `.json` imports, so it does).

- [ ] **Step 4: Commit**

```bash
git add web/lib/types.ts web/lib/costs.ts
git commit -m "feat(web): telephony types and costs exports"
```

---

### Task 5: Telephony render section

**Files:**
- Create: `web/components/categories/telephony-section.tsx`

**Interfaces:**
- Consumes: `TelephonyNumberRow[]` from Task 4.
- Produces: `<TelephonySection data={TelephonyNumberRow[]} />` for Task 6. Renders the **numbers** table (the customer-facing "what a number costs at cost" view); the `a2p_10dlc[]` fees are documentation-only and not rendered as a table here.

- [ ] **Step 1: Write the section component**

Modeled verbatim on `web/components/categories/stt-section.tsx` (a `'use client'` component defining a `ColumnDef[]` and rendering `<CategorySection>`). `usd(value, decimals)` and `priceRange(values, sigA, sigB, unit)` are from `@/lib/format`.

```tsx
'use client';

import type { ColumnDef } from '@tanstack/react-table';
import type { TelephonyNumberRow } from '@/lib/types';
import { CategorySection } from '../category-section';
import { VerifiedCell } from '../verified-cell';
import { priceRange, usd } from '@/lib/format';

const columns: ColumnDef<TelephonyNumberRow>[] = [
  {
    id: 'number',
    accessorKey: 'display_name',
    header: 'Number',
    cell: ({ row }) => (
      <div>
        <div style={{ fontWeight: 700 }}>{row.original.display_name}</div>
        <div style={{ fontSize: 13, marginTop: 2 }}>
          {row.original.country_code} · {row.original.number_type}
        </div>
      </div>
    ),
  },
  {
    id: 'price',
    accessorFn: (row) => Number(row.usd_per_month),
    header: '$/mo (at cost)',
    cell: ({ row }) => usd(row.original.usd_per_month, 2),
    sortingFn: 'basic',
    meta: { num: true, killer: true },
  },
  {
    id: 'verified',
    accessorKey: 'last_verified',
    header: 'Verified',
    cell: ({ row }) => <VerifiedCell date={row.original.last_verified} />,
    sortingFn: 'alphanumeric',
    meta: { num: true },
  },
];

export function TelephonySection({ data }: { data: TelephonyNumberRow[] }) {
  return (
    <CategorySection<TelephonyNumberRow>
      id="telephony"
      num="04"
      title={
        <>
          <em className="it">Phone</em> numbers
        </>
      }
      count={data.length}
      rangeLabel={priceRange(data.map((r) => r.usd_per_month), 2, 2, 'mo')}
      data={data}
      columns={columns}
      defaultSort={{ id: 'price', desc: false }}
    />
  );
}
```

- [ ] **Step 2: Typecheck via build**

Run: `pnpm site:build`
Expected: build succeeds. (Rendering is verified in Task 6 once wired into the page.)

- [ ] **Step 3: Commit**

```bash
git add web/components/categories/telephony-section.tsx
git commit -m "feat(web): telephony numbers render section"
```

---

### Task 6: Wire telephony into the main costs page

**Files:**
- Modify: `web/app/(dispatch)/page.tsx`

**Interfaces:**
- Consumes: `telephony` (Task 4), `TelephonySection` (Task 5).
- Produces: the telephony section renders on `hail.so/costs`, in the toolbar, in the summary strip, and in the programmatic-access URL list.

- [ ] **Step 1: Import telephony**

At the top of `web/app/(dispatch)/page.tsx`, extend the imports:

```tsx
import { llm, stt, tts, telephony } from '@/lib/costs';
import { TelephonySection } from '@/components/categories/telephony-section';
```

- [ ] **Step 2: Include telephony in providers/verified aggregates**

The page computes `totalModels` and a `providers` set over `[...llm.models, ...stt.models, ...tts.models]`. Telephony has no `provider` field and its rows are numbers, not models, so **do not** add it to `totalModels` or `providers` (those are model-centric stats). Leave those lines unchanged. (Rationale comment optional.)

- [ ] **Step 3: Add a summary stat tile**

In the summary strip (the `.stat` blocks, ~lines 90-115), add a telephony tile after the TTS tile, mirroring the others but reading `telephony.numbers.length`:

```tsx
<div className="stat">
  <div className="stat-n">{telephony.numbers.length}</div>
  <div className="stat-l">Numbers</div>
  <div className="stat-cap">{priceRange(telephony.numbers.map((r) => r.usd_per_month), 2, 2, 'mo')} · at cost</div>
</div>
```
(`priceRange` is already imported on this page for the other tiles; if not, add it to the `@/lib/format` import.)

- [ ] **Step 4: Add the toolbar category**

In the toolbar categories array (`[{ id: 'llm', label: 'LLM' }, ...]`), append:

```tsx
{ id: 'telephony', label: 'Numbers' },
```

- [ ] **Step 5: Render the section**

After `<TTSSection data={tts.models} />`, add:

```tsx
<TelephonySection data={telephony.numbers} />
```

- [ ] **Step 6: Add the programmatic-access URL**

In the `<ul>` listing raw JSON URLs (~lines 167-197), add a telephony `<li>` mirroring the others:

```tsx
<li>
  Telephony (number COGS + 10DLC fees):{' '}
  <code>https://raw.githubusercontent.com/hail-hq/hail/main/costs/telephony.json</code>
</li>
```

- [ ] **Step 7: Build and eyeball**

Run: `pnpm site:build && pnpm site:start` then open `http://localhost:3000/costs` (or the port printed).
Expected: build succeeds; the page shows a "Phone numbers" section with the US local ($1.15/mo) and US toll-free ($2.15/mo) rows, a "Numbers" toolbar chip, and the telephony JSON URL in the programmatic-access list. Stop the server after eyeballing.

- [ ] **Step 8: Commit**

```bash
git add "web/app/(dispatch)/page.tsx"
git commit -m "feat(web): render telephony section on the costs page"
```

---

### Task 7: Serve the telephony schema

**Files:**
- Modify: `web/app/schema/[name]/route.ts`

**Interfaces:**
- Consumes: `telephonySchema` (Task 4).
- Produces: `GET hail.so/costs/schema/telephony.json` serves the schema (its `$id`), matching the URL the schema declares.

- [ ] **Step 1: Register the schema**

In `web/app/schema/[name]/route.ts`, the `SCHEMAS` object maps `'llm.json' | 'stt.json' | 'tts.json'` to their schema objects and `generateStaticParams` derives from its keys. Add the telephony entry:

```typescript
import { llmSchema, sttSchema, ttsSchema, telephonySchema } from '@/lib/costs';

const SCHEMAS: Record<string, unknown> = {
  'llm.json': llmSchema,
  'stt.json': sttSchema,
  'tts.json': ttsSchema,
  'telephony.json': telephonySchema,
};
```

- [ ] **Step 2: Build and fetch**

Run: `pnpm site:build && pnpm site:start`, then `curl -s http://localhost:3000/costs/schema/telephony.json | head -5`.
Expected: the telephony schema JSON (starts with `"$schema"`/`"$id"`). Stop the server.

- [ ] **Step 3: Commit**

```bash
git add "web/app/schema/[name]/route.ts"
git commit -m "feat(web): serve telephony schema at its \$id url"
```

---

### Task 8: Reframe the costs README

**Files:**
- Modify: `costs/README.md`

**Interfaces:**
- Produces: the README documents telephony as a first-class category and stops asserting the `{ version, license, models[] }` envelope holds for *every* file.

- [ ] **Step 1: Widen the framing**

In `costs/README.md`:
- Line 3 intro ("cost and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech") → add telephony: "...text-to-speech, and telephony (phone-number COGS and A2P 10DLC fees)."
- The "common envelope `{ version, license, models[] }`" sentence → note the exception: "Model files (llm/stt/tts) share `{ version, license, models[] }`; `telephony.json` instead carries `numbers[]` and `a2p_10dlc[]`, since a phone number is not a model."
- The Canonical URLs list → add the telephony raw URL and its schema `$id` URL.
- Add a one-line note that the `web/` compare view and `costs.md` markdown export do **not** yet include telephony (out of scope; a later PR).

- [ ] **Step 2: Commit**

```bash
git add costs/README.md
git commit -m "docs(costs): document the telephony category and its two-array shape"
```

---

## Self-Review

**Spec coverage** (against the `costs/telephony.json` section of the design spec):
- `costs/telephony.json` (numbers[] + a2p_10dlc[] only, no SMS wholesale) → Task 1. ✓
- `costs/schema/telephony.schema.json` modeled on stt → Task 1. ✓
- `costs-validate.yml` step + cross-field loop → Task 2 (loop deliberately not extended — telephony is not model-shaped; documented). ✓
- `check-stale.mjs` generalized past hardcoded `models[]`, unknown file = hard error → Task 3, incl. the two required tests (telephony checked; unknown errors). ✓
- `web/lib/costs.ts` export → Task 4; `TelephonySection` → Task 5; `page.tsx` render + totalModels note → Task 6; README reframe → Task 8. ✓
- Schema served at `$id` → Task 7 (implied by the reframe/public-render requirement). ✓

**Placeholder scan:** none — every step carries concrete code, a concrete command, or an exact edit target.

**Type consistency:** `TelephonyNumberRow`/`TelephonyFeeRow`/`TelephonyFile` defined in Task 4 are used consistently in Tasks 5–7. `telephony.numbers` (not `.models`) used everywhere telephony is read. `usd_per_month` is a decimal string in the schema (Task 1), typed `string` (Task 4), and `Number(...)`-coerced only for sorting (Task 5) and formatted via `usd()` for display.

**Scope note:** The design spec's testing bullet "telephony-costs.ts" belongs to **Plan 2** (it lives in hail-website and fetches this file), not here. This plan produces the data + public surface it consumes.
