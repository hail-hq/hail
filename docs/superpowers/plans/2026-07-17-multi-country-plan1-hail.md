# Multi-country numbers — Plan 1 (hail): data, sync, catalog, allow-list

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `costs/telephony.json` a broad, capability-accurate catalog synced from Twilio, and make it the acquire allow-list — the hail API rejects any `(country, type)` it can't price, so no held number can ever go unbilled.

**Architecture:** A Node sync script rewrites `telephony.json`'s `numbers[]` from Twilio's own numbers dataset (price + `voice`/`sms`/`mms` per row), gated behind a scheduled workflow that opens a PR. A new `hailhq.core.telephony_catalog` module reads the committed file at runtime; the acquire endpoint uses it to 422 unlisted combinations. Adds the `national` number type (migration + schema). The `/costs` page renders capabilities across all countries.

**Tech Stack:** Node ESM (sync, mirrors `scripts/costs/check-stale.mjs`), Python 3.12 / FastAPI / SQLAlchemy / Alembic (catalog, migration, acquire), JSON Schema, Next.js (`web/`).

## Global Constraints

- Repo/worktree: `/Users/r/playground/hail/.claude/worktrees/multi-country-numbers`, branch `feat/multi-country-numbers` (off `main`).
- **Never merge locally to `main`.** Integration is via GitHub PR. Commit to the feature branch only.
- **No `Co-Authored-By: Claude` (or any AI-attribution) trailer** in commit messages.
- **OpenAPI**: after any change to a route or a request/response schema, regenerate `openapi/openapi.yaml` in the same PR (the `NumberType` change alters `NumberAcquireRequest`'s schema, so this plan regenerates it). Command: run the API locally then `curl -s http://localhost:8080/openapi.json | python -c "import json,sys,yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" > openapi/openapi.yaml`.
- **Provider-adapter layering**: Twilio SDK usage stays in `core/hailhq/core/providers/`. The sync script is a standalone Node script and does not import `core`.
- Number types are exactly `local | mobile | toll_free | national`. ISO country codes are alpha-2 uppercase. Prices are decimal strings.
- **Data-safety invariant (the whole point):** the sync must NEVER write an empty or short `numbers[]`. If the fetch yields `< 40` countries, abort and leave the committed file untouched — a truncated allow-list would make held numbers unbillable.
- Reference data (for fixtures / acceptance): `…/scratchpad/twilio-numbers.csv` (the real Twilio CSV, 106 rows) and `…/scratchpad/numbers-data.json` (the parsed expected shape). Path prefix: `/private/tmp/claude-501/-Users-r-playground/003eca1d-9725-42a2-b57a-817f889f44fb/scratchpad/`.
- Python: `ruff` + `black` + `mypy` + `pytest` in CI; run `cd api && uv run pytest`, `uvx black`, `uvx ruff check`. Commits are Conventional Commits.

---

### Task 1: Add the `national` number type (schema literal + DB migration)

**Files:**
- Modify: `core/hailhq/core/schemas.py:228` (the `NumberType` literal)
- Create: `api/migrations/versions/0035_number_type_national.py`
- Test: `core/tests/test_schemas.py` (or a new `core/tests/test_number_type.py`)

**Interfaces:**
- Produces: `NumberType = Literal["local","mobile","toll_free","national"]`, and a widened `phone_numbers_number_type_check` CHECK constraint. Consumed by the acquire endpoint (Task 6), the provider (`getattr(country_ctx, "national")` already works), and the catalog.

- [ ] **Step 1: Write a failing test that `national` is a valid `NumberType`**

Add to `core/tests/test_number_type.py`:

```python
from hailhq.core.schemas import NumberAcquireRequest


def test_national_is_an_accepted_number_type():
    req = NumberAcquireRequest(country_code="JP", number_type="national")
    assert req.number_type == "national"
```

- [ ] **Step 2: Run it — expect fail**

Run: `cd core && uv run pytest tests/test_number_type.py -q`
Expected: FAIL — pydantic `ValidationError` ("Input should be 'local', 'mobile' or 'toll_free'").

- [ ] **Step 3: Widen the literal**

In `core/hailhq/core/schemas.py` line 228:
```python
NumberType = Literal["local", "mobile", "toll_free", "national"]
```
(`NumberType` is re-exported via `providers/voice/base.py`'s `__all__`, so this one edit propagates.)

- [ ] **Step 4: Run it — expect pass**

Run: `cd core && uv run pytest tests/test_number_type.py -q`
Expected: PASS.

- [ ] **Step 5: Create the migration**

Create `api/migrations/versions/0035_number_type_national.py`, mirroring the CHECK-widen pattern of `0011_usage_events_email_inbound_channel.py` (drop + re-add; Postgres can't widen a CHECK in place; a value-add is safe with no pre-flight since existing rows satisfy the wider set):

```python
"""Add 'national' to phone_numbers.number_type.

Twilio offers a `national` number type for several countries (Japan, Brazil,
Romania, Czech Republic). Postgres can't widen a CHECK in place, so this drops
and re-adds phone_numbers_number_type_check. Existing rows only ever used
local/mobile/toll_free, a subset of the new set, so the re-add is safe with no
pre-flight check.

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("phone_numbers_number_type_check", "phone_numbers", type_="check")
    op.create_check_constraint(
        "phone_numbers_number_type_check",
        "phone_numbers",
        "number_type IN ('local','mobile','toll_free','national')",
    )


def downgrade() -> None:
    op.drop_constraint("phone_numbers_number_type_check", "phone_numbers", type_="check")
    op.create_check_constraint(
        "phone_numbers_number_type_check",
        "phone_numbers",
        "number_type IN ('local','mobile','toll_free')",
    )
```

Also update the model's inline CHECK in `core/hailhq/core/models.py` (the `phone_numbers_number_type_check` `CheckConstraint`, ~line 356) so `Base.metadata.create_all` (used by tests) matches:
```python
CheckConstraint(
    "number_type IN ('local','mobile','toll_free','national')",
    name="phone_numbers_number_type_check",
),
```

- [ ] **Step 6: Apply + verify the migration**

Run: `cd api && uv run alembic upgrade head` (against the local dev DB — see root CLAUDE.md dev commands for bringing up Postgres).
Expected: applies `0035` with no error. Then `uv run alembic downgrade -1 && uv run alembic upgrade head` round-trips cleanly.

- [ ] **Step 7: Lint + commit**

Run: `uvx ruff check core/hailhq/core/schemas.py api/migrations/versions/0035_number_type_national.py && uvx black --check core/hailhq/core/schemas.py core/hailhq/core/models.py api/migrations/versions/0035_number_type_national.py core/tests/test_number_type.py` (run `uvx black` without `--check` to fix if needed).
```bash
git add core/hailhq/core/schemas.py core/hailhq/core/models.py api/migrations/versions/0035_number_type_national.py core/tests/test_number_type.py
git commit -m "feat(core): add 'national' number type (schema literal + migration)"
```

---

### Task 2: Extend the telephony schema with capabilities

**Files:**
- Modify: `costs/schema/telephony.schema.json`
- Modify: `web/lib/types.ts` (the `TelephonyNumberRow` type + `VerificationMethod`)
- Test: `check-jsonschema` runs (validation is the test)

**Interfaces:**
- Produces: `numbers[]` rows now require `voice`/`sms`/`mms` (booleans) and `dial_code` (string), allow `number_type: national`, and accept `verification_method: "carrier-sync"`. A row with neither `voice` nor `sms` true is invalid.

- [ ] **Step 1: Extend the schema**

In `costs/schema/telephony.schema.json`, in the `number` object definition:
- Add `"national"` to the `number_type` enum: `"enum": ["local","mobile","toll_free","national"]`.
- Add to `properties`: `"voice": {"type":"boolean"}`, `"sms": {"type":"boolean"}`, `"mms": {"type":"boolean"}`, `"dial_code": {"type":"string","minLength":1}`.
- Add `"voice"`, `"sms"`, `"mms"`, `"dial_code"` to the number's `required` array.
- Add an `anyOf` on the number object requiring at least one of voice/sms true:
  ```json
  "anyOf": [ { "required": ["voice"], "properties": { "voice": { "const": true } } },
             { "required": ["sms"],   "properties": { "sms":   { "const": true } } } ]
  ```
- Add `"carrier-sync"` to **both** `verification_method` enums (the `number` def and the `fee` def — they are inlined separately): `"enum": ["manual-confirmed","community-pr","carrier-sync"]`.

- [ ] **Step 2: Validate the (still US-only) file fails until it has the new fields**

Run: `pipx run check-jsonschema==0.29.4 --schemafile costs/schema/telephony.schema.json costs/telephony.json`
Expected: FAIL — the existing US rows lack `voice`/`sms`/`mms`/`dial_code` now-required fields. (This is expected; Task 4 replaces the data via the sync. To keep the tree valid between tasks, hand-add the four fields to the two existing US rows now: US local `voice:true,sms:true,mms:true,dial_code:"1"`; US toll_free same. Re-run → PASS.)

- [ ] **Step 3: Mirror the type in `web/lib/types.ts`**

- Add `'carrier-sync'` to `VerificationMethod`: `export type VerificationMethod = 'manual-confirmed' | 'community-pr' | 'carrier-sync';`
- Extend `TelephonyNumberRow`:
  ```ts
  export interface TelephonyNumberRow extends TelephonyProvenance {
    country_code: string;
    number_type: 'local' | 'mobile' | 'toll_free' | 'national';
    display_name: string;
    dial_code: string;
    usd_per_month: string;
    voice: boolean;
    sms: boolean;
    mms: boolean;
  }
  ```

- [ ] **Step 4: Typecheck web + commit**

Run: `pnpm --filter @hail-hq/web build` (from repo root) — expect success.
```bash
git add costs/schema/telephony.schema.json costs/telephony.json web/lib/types.ts
git commit -m "feat(costs): telephony schema — number capabilities, national, carrier-sync"
```

---

### Task 3: The Twilio → telephony.json sync script

**Files:**
- Create: `scripts/costs/sync-telephony.mjs`
- Create: `scripts/costs/sync-telephony.test.mjs`
- Modify: root `package.json` (add `costs:sync-telephony`)

**Interfaces:**
- Produces: `costs/telephony.json` rewritten from Twilio's numbers CSV. Exports a pure `mapCsvToNumbers(csvText) -> {rows, countryCount}` (testable) and a `main()` guarded by the `import.meta.url === pathToFileURL(process.argv[1]).href` idiom (mirrors `check-stale.mjs`).
- The CSV columns (real, from the reference file): `ISO, Country, Country Code, Phone Number Type, Voice Enabled, Trunking Enabled, SMS Enabled, MMS Enabled, ..., Phone Number Price / month, ...`. `Yes/No` → booleans; `Local|Mobile|Toll Free|National` → `local|mobile|toll_free|national`.

- [ ] **Step 1: Write failing tests for the pure mapper**

Create `scripts/costs/sync-telephony.test.mjs` (`node:test` + `node:assert/strict`, mirroring `check-stale.test.mjs`):

```javascript
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mapCsvToNumbers } from './sync-telephony.mjs';

const CSV =
  'ISO,Country,Country Code,Phone Number Type,Voice Enabled,Trunking Enabled,SMS Enabled,MMS Enabled,Domestic Voice Only,Domestic SMS Only,Phone Number Price / month\n' +
  'SE,Sweden,46,Mobile,No,No,Yes,No,N/A,No,3.00\n' +
  'US,United States,1,Local,Yes,Yes,Yes,Yes,No,N/A,1.15\n' +
  'GB,United Kingdom,44,Toll Free,Yes,Yes,No,No,Yes,N/A,2.15\n' +
  'XX,Nowhere,999,Local,No,No,No,No,N/A,N/A,5.00\n' + // neither voice nor sms — dropped
  'JP,Japan,81,National,Yes,Yes,No,No,Yes,N/A,4.50\n';

test('maps capabilities and price, dropping no-capability rows', () => {
  const { rows, countryCount } = mapCsvToNumbers(CSV);
  const se = rows.find((r) => r.country_code === 'SE');
  assert.equal(se.number_type, 'mobile');
  assert.equal(se.voice, false);
  assert.equal(se.sms, true);
  assert.equal(se.mms, false);
  assert.equal(se.usd_per_month, '3.00');
  assert.equal(se.dial_code, '46');
  const us = rows.find((r) => r.country_code === 'US');
  assert.deepEqual([us.voice, us.sms, us.mms], [true, true, true]);
  const jp = rows.find((r) => r.country_code === 'JP');
  assert.equal(jp.number_type, 'national');
  // XX row (no voice, no sms) is dropped
  assert.equal(rows.some((r) => r.country_code === 'XX'), false);
  assert.equal(countryCount, 4); // SE, US, GB, JP
});

test('prices are decimal strings, not numbers', () => {
  const { rows } = mapCsvToNumbers(CSV);
  assert.equal(typeof rows[0].usd_per_month, 'string');
});
```

- [ ] **Step 2: Run — expect fail**

Run: `node --test scripts/costs/sync-telephony.test.mjs`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the sync**

Create `scripts/costs/sync-telephony.mjs`:

```javascript
import { readFile, writeFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, '..', '..');
const TELEPHONY_JSON = join(REPO_ROOT, 'costs', 'telephony.json');

// Twilio's machine-readable numbers dataset (the feed behind the pricing pages).
// The hashed path can rotate; override with SYNC_CSV_URL if Twilio moves it.
const CSV_URL =
  process.env.SYNC_CSV_URL ||
  'https://www.twilio.com/content/dam/twilio-com/pricing-data/en/csv/PMded94a0dae30eaaec0f115f22859bd38_SiteNumbersPricing.csv';

const TYPE_MAP = { Local: 'local', Mobile: 'mobile', 'Toll Free': 'toll_free', National: 'national' };
const COUNTRY_FLOOR = 40; // never shrink the allow-list below this — see plan's data-safety invariant

// Minimal CSV parser adequate for Twilio's quote-free numeric feed.
function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/);
  const header = lines[0].split(',');
  return lines.slice(1).map((line) => {
    const cells = line.split(',');
    return Object.fromEntries(header.map((h, i) => [h.trim(), (cells[i] ?? '').trim()]));
  });
}

export function mapCsvToNumbers(csvText) {
  const rows = [];
  const countries = new Set();
  for (const r of parseCsv(csvText)) {
    const number_type = TYPE_MAP[r['Phone Number Type']];
    if (!number_type) continue;
    const voice = r['Voice Enabled'] === 'Yes';
    const sms = r['SMS Enabled'] === 'Yes';
    const mms = r['MMS Enabled'] === 'Yes';
    if (!voice && !sms) continue; // a number must do something
    const priceRaw = r['Phone Number Price / month'];
    if (!/^\d+(\.\d+)?$/.test(priceRaw)) continue;
    const iso = r['ISO'];
    countries.add(iso);
    rows.push({
      country_code: iso,
      number_type,
      display_name: `${r['Country']} ${r['Phone Number Type'].toLowerCase()}`,
      dial_code: r['Country Code'],
      usd_per_month: priceRaw,
      voice, sms, mms,
    });
  }
  rows.sort((a, b) =>
    a.country_code.localeCompare(b.country_code) || a.number_type.localeCompare(b.number_type));
  return { rows, countryCount: countries.size };
}

async function main() {
  const res = await fetch(CSV_URL);
  if (!res.ok) throw new Error(`Twilio CSV fetch failed: ${res.status} (${CSV_URL})`);
  const csv = await res.text();
  const { rows, countryCount } = mapCsvToNumbers(csv);
  if (countryCount < COUNTRY_FLOOR) {
    throw new Error(`sync aborted: only ${countryCount} countries (< ${COUNTRY_FLOOR}); refusing to shrink the allow-list`);
  }
  const existing = JSON.parse(await readFile(TELEPHONY_JSON, 'utf-8'));
  const today = new Date().toISOString().slice(0, 10);
  const prevByKey = new Map(
    (existing.numbers || []).map((n) => [`${n.country_code}:${n.number_type}`, n]));
  const numbers = rows.map((n) => {
    const prev = prevByKey.get(`${n.country_code}:${n.number_type}`);
    const changed = !prev || prev.usd_per_month !== n.usd_per_month
      || prev.voice !== n.voice || prev.sms !== n.sms || prev.mms !== n.mms;
    return {
      ...n,
      last_verified: today,
      last_changed_at: changed ? today : (prev.last_changed_at || today),
      verification_method: 'carrier-sync',
      verified_by: 'twilio-sync',
      source_url: CSV_URL,
    };
  });
  const out = { ...existing, numbers };
  await writeFile(TELEPHONY_JSON, JSON.stringify(out, null, 2) + '\n');
  console.log(`wrote ${numbers.length} rows across ${countryCount} countries`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => { console.error(err); process.exit(1); });
}
```

- [ ] **Step 4: Run tests — expect pass**

Run: `node --test scripts/costs/sync-telephony.test.mjs`
Expected: PASS (2 tests).

- [ ] **Step 5: Add the npm script**

In root `package.json`, add to `scripts`:
```json
"costs:sync-telephony": "node scripts/costs/sync-telephony.mjs",
```

- [ ] **Step 6: Commit**

```bash
git add scripts/costs/sync-telephony.mjs scripts/costs/sync-telephony.test.mjs package.json
git commit -m "feat(costs): Twilio->telephony.json sync with capabilities"
```

---

### Task 4: Seed the broad catalog (run the sync)

**Files:**
- Modify: `costs/telephony.json` (regenerated by the sync)

**Interfaces:**
- Produces: `telephony.json` with ~65 countries / ~106 rows, capabilities + prices, schema-valid. This is the first broad allow-list.

- [ ] **Step 1: Run the sync**

Run: `pnpm costs:sync-telephony`
Expected: `wrote ~106 rows across ~65 countries`. If Twilio's CSV URL 404s (the hash rotated), find the current CSV link on `https://www.twilio.com/en-us/sms/pricing/us` (or a country pricing page's "download" data link) and re-run with `SYNC_CSV_URL=<url> pnpm costs:sync-telephony`.

- [ ] **Step 2: Validate**

Run: `pnpm costs:validate`
Expected: all costs files valid, including telephony with the new capability fields. If a row fails (e.g. a country with an unmapped type), fix the mapper and re-run.

- [ ] **Step 3: Sanity-check against the reference**

Run: `node -e "const d=require('./costs/telephony.json'); const se=d.numbers.find(n=>n.country_code==='SE'); console.log(JSON.stringify(se))"`
Expected: Sweden = one `mobile` row, `voice:false, sms:true` — the canary. If Sweden shows voice-capable, the sync is wrong; stop and fix.

- [ ] **Step 4: Commit the seeded catalog**

```bash
git add costs/telephony.json
git commit -m "feat(costs): seed broad multi-country telephony catalog"
```

---

### Task 5: Scheduled sync-PR workflow

**Files:**
- Create: `.github/workflows/costs-sync-telephony.yml`

**Interfaces:**
- Produces: a scheduled + manual workflow that runs the sync and opens/updates a PR with any change — never auto-commits to `main`.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/costs-sync-telephony.yml` (schedule mirrors `costs-stale.yml`; uses `peter-evans/create-pull-request` — the repo's first PR-opening action):

```yaml
name: costs-sync-telephony

on:
  schedule:
    - cron: "0 8 * * 1"   # Mondays 08:00 UTC, before the stale check
  workflow_dispatch: {}

permissions:
  contents: write
  pull-requests: write

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: "20"
      - name: Sync telephony catalog from Twilio
        run: node scripts/costs/sync-telephony.mjs
      - name: Validate
        run: pipx run check-jsonschema==0.29.4 --schemafile costs/schema/telephony.schema.json costs/telephony.json
      - name: Open PR on change
        uses: peter-evans/create-pull-request@v6
        with:
          branch: costs/telephony-sync
          title: "chore(costs): telephony catalog sync"
          commit-message: "chore(costs): telephony catalog sync"
          body: |
            Automated sync of `costs/telephony.json` from Twilio's numbers dataset.
            Review price/capability changes before merging — this is the acquire allow-list.
          labels: costs-sync
```

- [ ] **Step 2: Lint the YAML**

Run: `npx --yes yaml-lint .github/workflows/costs-sync-telephony.yml`
Expected: valid. (No CI runs it here; verification is a manual `workflow_dispatch` after merge — record as an operator step.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/costs-sync-telephony.yml
git commit -m "ci(costs): scheduled telephony sync that opens a PR"
```

---

### Task 6: `telephony_catalog` module + bundle `costs/` into the API image

**Files:**
- Create: `core/hailhq/core/telephony_catalog.py`
- Create: `core/tests/test_telephony_catalog.py`
- Modify: `api/Dockerfile` (copy `costs/` into the runtime image)

**Interfaces:**
- Produces: `is_acquirable(country_code, number_type) -> bool`, `price_usd_per_month(country_code, number_type) -> Decimal | None`, `capabilities(country_code, number_type) -> dict | None`. Consumed by the acquire endpoint (Task 7). Reads the committed `costs/telephony.json`; **raises on a missing file** (never silently allow).

- [ ] **Step 1: Write failing tests**

Create `core/tests/test_telephony_catalog.py`:

```python
from decimal import Decimal
import json
import pathlib

import pytest

from hailhq.core import telephony_catalog


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    data = {
        "version": 2, "license": "CC-BY-4.0",
        "numbers": [
            {"country_code": "SE", "number_type": "mobile", "usd_per_month": "3.00",
             "voice": False, "sms": True, "mms": False},
            {"country_code": "US", "number_type": "local", "usd_per_month": "1.15",
             "voice": True, "sms": True, "mms": True},
        ],
        "a2p_10dlc": [],
    }
    p = tmp_path / "telephony.json"
    p.write_text(json.dumps(data))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(p))
    telephony_catalog._load.cache_clear()  # reset the lru_cache between tests
    return telephony_catalog


def test_is_acquirable(catalog):
    assert catalog.is_acquirable("SE", "mobile") is True
    assert catalog.is_acquirable("SE", "local") is False   # not listed
    assert catalog.is_acquirable("ZZ", "local") is False


def test_price_and_capabilities(catalog):
    assert catalog.price_usd_per_month("US", "local") == Decimal("1.15")
    assert catalog.price_usd_per_month("SE", "local") is None
    assert catalog.capabilities("SE", "mobile") == {"voice": False, "sms": True, "mms": False}


def test_missing_file_raises_not_silently_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(tmp_path / "nope.json"))
    telephony_catalog._load.cache_clear()
    with pytest.raises(FileNotFoundError):
        telephony_catalog.is_acquirable("US", "local")
```

- [ ] **Step 2: Run — expect fail**

Run: `cd core && uv run pytest tests/test_telephony_catalog.py -q`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `core/hailhq/core/telephony_catalog.py`:

```python
"""Read-only view of costs/telephony.json — the number price + capability
catalog and the acquire allow-list. The same file the rater (hail-website) and
the /costs page read, so the three can never disagree about what's acquirable.

A missing file raises rather than silently allowing/denying: an acquire guard
that fails open would let unpriced numbers through and break "price every
number"; one that fails closed would block all acquisition. Surfacing the error
forces the deploy to be fixed (the file must be bundled — see api/Dockerfile).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

__all__ = ["is_acquirable", "price_usd_per_month", "capabilities"]

# In the API image costs/ is copied to /app/costs (see api/Dockerfile); in dev
# the module sits at core/hailhq/core/ so parents[3] is the repo root. An env
# var overrides both (tests, alternate layouts).
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "costs" / "telephony.json"


def _path() -> Path:
    return Path(os.environ.get("HAIL_TELEPHONY_CATALOG_PATH", str(_DEFAULT_PATH)))


@lru_cache(maxsize=1)
def _load() -> dict[tuple[str, str], dict]:
    raw = json.loads(_path().read_text())
    return {(n["country_code"], n["number_type"]): n for n in raw["numbers"]}


def is_acquirable(country_code: str, number_type: str) -> bool:
    return (country_code, number_type) in _load()


def price_usd_per_month(country_code: str, number_type: str) -> Decimal | None:
    row = _load().get((country_code, number_type))
    return Decimal(row["usd_per_month"]) if row else None


def capabilities(country_code: str, number_type: str) -> dict | None:
    row = _load().get((country_code, number_type))
    if not row:
        return None
    return {"voice": row["voice"], "sms": row["sms"], "mms": row["mms"]}
```

- [ ] **Step 4: Run — expect pass**

Run: `cd core && uv run pytest tests/test_telephony_catalog.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Bundle `costs/` into the API runtime image**

In `api/Dockerfile`, in the **runtime** stage (where `alembic.ini`/`migrations` are copied), add:
```dockerfile
COPY --chown=hail:hail costs /app/costs
```
This makes `/app/costs/telephony.json` present at runtime; `_DEFAULT_PATH` resolves to it (module at `/app/core/hailhq/core/` → `parents[3]` = `/app`).

- [ ] **Step 6: Lint + commit**

Run: `uvx ruff check core/hailhq/core/telephony_catalog.py && uvx black --check core/hailhq/core/telephony_catalog.py core/tests/test_telephony_catalog.py`
```bash
git add core/hailhq/core/telephony_catalog.py core/tests/test_telephony_catalog.py api/Dockerfile
git commit -m "feat(core): telephony_catalog allow-list reader; bundle costs into api image"
```

---

### Task 7: Acquire-endpoint allow-list guard

**Files:**
- Modify: `api/hailhq/api/routes/numbers.py` (the `acquire_number` handler)
- Modify: `openapi/openapi.yaml` (regen — `NumberType` gained `national`)
- Test: `api/tests/test_numbers_api.py`

**Interfaces:**
- Consumes: `telephony_catalog.is_acquirable` (Task 6), `unprocessable` (existing, `hailhq.api.errors`), the widened `NumberType` (Task 1).
- Produces: `POST /numbers` returns **422** for an unlisted `(country_code, number_type)`, before any provider call.

- [ ] **Step 1: Write failing tests**

Add to `api/tests/test_numbers_api.py` (mirror the existing acquire tests' fixtures — `client`, `org_and_key`, `voice_provider_mock`):

```python
async def test_acquire_rejects_unlisted_country_type(client, org_and_key, voice_provider_mock):
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "ZZ", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422, resp.text
    voice_provider_mock.acquire_number.assert_not_awaited()  # guarded before the provider


async def test_acquire_allows_listed_country_type(client, org_and_key, voice_provider_mock):
    _, _, plaintext = org_and_key
    # US/local is in the seeded catalog; the provider mock returns a fake number.
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    voice_provider_mock.acquire_number.assert_awaited_once()
```

(If the test suite sets `HAIL_TELEPHONY_CATALOG_PATH` to a fixture, point it at a small catalog containing `US/local`. Otherwise the tests use the committed `costs/telephony.json`, which has US/local after Task 4 — confirm and, if needed, add a conftest fixture setting the env var to a test catalog with `US/local` and without `ZZ/local`.)

- [ ] **Step 2: Run — expect fail**

Run: `cd api && uv run pytest tests/test_numbers_api.py -k acquire_rejects -v`
Expected: FAIL — currently the unlisted combo reaches the provider (503 or a mock result), not 422.

- [ ] **Step 3: Add the guard**

In `acquire_number` (`api/hailhq/api/routes/numbers.py`), before the `provider.acquire_number(...)` call, add:

```python
    from hailhq.core import telephony_catalog

    if not telephony_catalog.is_acquirable(body.country_code, body.number_type):
        raise unprocessable(
            f"we don't offer a {body.number_type} number in {body.country_code} yet",
            loc=["body", "number_type"],
        )
```
(Ensure `unprocessable` is imported at the top — it already is, used by `enable_sms`.)

- [ ] **Step 4: Run — expect pass**

Run: `cd api && uv run pytest tests/test_numbers_api.py -k acquire -v`
Expected: PASS (reject → 422 without provider call; allow → 201 with provider call).

- [ ] **Step 5: Regenerate OpenAPI**

Start the API locally (`cd api && uv run uvicorn hailhq.api.main:app --port 8080`), then in another shell:
```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json,sys,yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```
Verify the `NumberAcquireRequest.number_type` enum in `openapi/openapi.yaml` now includes `national`. Stop the server. Run the CI parity check (`.github/workflows/openapi-check.yml`'s in-process compare) to confirm no drift.

- [ ] **Step 6: Lint + commit**

```bash
git add api/hailhq/api/routes/numbers.py api/tests/test_numbers_api.py openapi/openapi.yaml
git commit -m "feat(api): reject acquiring an unlisted country/number-type (allow-list)"
```

---

### Task 8: `/costs` — render capabilities across all countries

**Files:**
- Modify: `web/components/categories/telephony-section.tsx`

**Interfaces:**
- Consumes: `telephony.numbers` rows now carrying `voice`/`sms`/`mms`/`number_type` incl. `national`.
- Produces: the public `/costs` telephony table shows Calls/Texts/MMS capability per row across all countries.

- [ ] **Step 1: Add capability columns**

In `web/components/categories/telephony-section.tsx`, extend the `columns: ColumnDef<TelephonyNumberRow>[]` with capability columns after `price` (mirror the existing `cell` style; `✓`/`—`):

```tsx
{ id: 'calls', header: 'Calls', accessorKey: 'voice',
  cell: ({ row }) => (row.original.voice ? '✓' : '—'), meta: { num: true } },
{ id: 'texts', header: 'Texts', accessorKey: 'sms',
  cell: ({ row }) => (row.original.sms ? '✓' : '—'), meta: { num: true } },
{ id: 'mms', header: 'MMS', accessorKey: 'mms',
  cell: ({ row }) => (row.original.mms ? '✓' : '—'), meta: { num: true } },
```
The section already sorts by price and scales to many rows via `CategorySection`; no other change needed.

- [ ] **Step 2: Build + eyeball**

Run: `pnpm --filter @hail-hq/web build` then `pnpm --filter @hail-hq/web start` and open `/costs`.
Expected: the Numbers table lists ~65 countries with Calls/Texts/MMS columns; Sweden shows Texts ✓, Calls —. Stop the server.

- [ ] **Step 3: Commit**

```bash
git add web/components/categories/telephony-section.tsx
git commit -m "feat(web): show number capabilities across all countries on /costs"
```

---

## Self-Review

**Spec coverage:**
- `telephony.json` = allow-list → Task 7 (acquire guard) + Task 6 (catalog). ✓
- Capabilities first-class → Task 2 (schema) + Task 3/4 (sync populates). ✓
- Twilio sync, PR-gated, never empty/short → Task 3 (`COUNTRY_FLOOR` abort) + Task 5 (PR workflow). ✓
- Whole-month billing unchanged; rater untouched → not in this plan (hail-website); capability fields don't affect the rater (verified in recon). ✓
- `national` migration → Task 1. ✓
- `/costs` capabilities → Task 8. ✓
- Deploy: the seeded `telephony.json` (Task 4) must be on `main`'s raw URL before hail-website (Plan 2) builds — covered by PR-then-merge before Plan 2.

**Placeholder scan:** none — every step has concrete code/commands. The one conditional (Task 4 Step 1 CSV-URL fallback, Task 7 Step 1 catalog-fixture note) names the exact action, not a vague "handle it."

**Type consistency:** `NumberType` widened once (Task 1) and used in the acquire request, provider, and OpenAPI. `telephony_catalog` returns `Decimal`/`dict|None` consistently (Task 6) and is called with `(country_code, number_type)` in Task 7. Schema capability field names (`voice`/`sms`/`mms`/`dial_code`) match the TS type (Task 2), the sync output (Task 3), and the catalog reads (Task 6).

**Cross-plan note:** Plan 2 (hail-website picker) depends on this plan's `telephony.json` (capabilities) being live on `main`'s raw URL, and on the acquire endpoint accepting `number_type` (it already does; this plan only adds the guard + `national`).
