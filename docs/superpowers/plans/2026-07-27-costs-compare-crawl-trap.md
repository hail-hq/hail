# Costs Compare Crawl Trap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unbounded `?m=` comparison URL space with 96 prerendered pair pages derived from a `featured` flag in `costs/*.json`, and make `/costs/compare` fully static.

**Architecture:** A new optional `featured` boolean in the costs schema seeds a finite set of within-category model pairs. `web/lib/featured.ts` derives slugs from that flag and is consumed by a static `[pair]` route and by `app/sitemap.ts`. `/costs/compare` becomes `force-static` with selection moved to a client component, and its crawlable `<a href>` pills become `<button>`s. `costs/featured.lock.json` is the contract between the Next app and a Node CI guard, since `allowJs: false` prevents sharing a module across that boundary.

**Tech Stack:** Next.js 16 (App Router, `basePath: '/costs'`), React 19, TypeScript 5.5 (strict), Node's built-in `node:test`, `check-jsonschema`, prettier 3.8.

## Global Constraints

- **Branch:** `feat/costs-compare-static-pages` (gitflow). All work happens in a git worktree.
- **Commits:** Conventional Commits (`type(scope): description`). **Never** add a `Co-Authored-By` trailer or any AI-attribution line.
- **Schema validation:** `pnpm costs:validate` is broken on macOS hosts by a pipx/Python 3.14 ABI fault. Use `/Users/r/.local/bin/check-jsonschema --schemafile <schema> <data>` locally. CI is unaffected.
- **Costs data formatting:** after any edit to `costs/*.json`, run `pnpm exec prettier --write costs/<file>.json` from the repo root. Never rewrite these files with `jq`, `python`, or `node` — re-serializing expands arrays and escapes non-ASCII, producing a 1,000+ line formatting diff.
- **String sorting must be deterministic.** Use bare `.sort()` (UTF-16 code-unit order) for slug and pair ordering, never `localeCompare` — it is locale-dependent and would produce different route sets on different machines.
- **URL construction:** build absolute URLs with `new URL(path, SITE_ORIGIN)`, never string concatenation. Per the repo invariant in `CLAUDE.md`, URLs are not strings.
- **No new test framework in `web/`.** The repo has no web test runner and adding one is out of scope. Next-side correctness is enforced by build-time assertions and `pnpm site:build`.
- **Slug format:** the two `model_id`s sorted with `.sort()`, joined by `-vs-`. Pairs are within-category only.
- **Featured set:** 12 LLM, 6 STT, 6 TTS → 66 + 15 + 15 = **96 pages**.

---

## File Structure

| Path                                         | Responsibility                                                                                           |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `costs/schema/{llm,stt,tts}.schema.json`     | Declare the optional `featured` boolean (v2.3)                                                           |
| `costs/{llm,stt,tts}.json`                   | Carry `featured: true` on the 24 seed rows                                                               |
| `costs/featured.lock.json`                   | Committed snapshot of generated slugs; the contract between the Next app and CI                          |
| `scripts/costs/check-featured.mjs`           | CI guard + lockfile generator (`--write`)                                                                |
| `scripts/costs/check-featured.test.mjs`      | Unit tests for the guard's pure functions                                                                |
| `web/lib/types.ts`                           | Add `featured?: boolean` to `CommonFields`                                                               |
| `web/lib/featured.ts`                        | Single source for featured models, pair slugs, slug→pair map; asserts against the lockfile at build time |
| `web/app/(dispatch)/compare/[pair]/page.tsx` | The 96 static comparison pages                                                                           |
| `web/components/deprecation-notice.tsx`      | Banner + successor link for deprecated featured models                                                   |
| `web/components/compare-picker.tsx`          | Client-side selection for `/costs/compare`                                                               |
| `web/app/(dispatch)/compare/page.tsx`        | Becomes a static shell that renders the picker                                                           |
| `web/app/sitemap.ts`                         | Serves `/costs/sitemap.xml`                                                                              |
| `web/next.config.ts`                         | Canonical-host 308 redirect                                                                              |
| `docs/operations/refresh-costs.md`           | Featured-models maintenance procedure                                                                    |

---

## Task 1: `featured` flag — schema, types, seed data

**Files:**

- Modify: `costs/schema/llm.schema.json`, `costs/schema/stt.schema.json`, `costs/schema/tts.schema.json`
- Modify: `costs/llm.json`, `costs/stt.json`, `costs/tts.json`
- Modify: `web/lib/types.ts`

**Interfaces:**

- Consumes: nothing.
- Produces: an optional `featured?: boolean` on every model row, set to `true` on exactly 24 rows. Task 2 and Task 3 both read it.

- [ ] **Step 1: Add `featured` to all three schemas**

In each of `costs/schema/{llm,stt,tts}.schema.json`, find the `properties` object inside `models.items` and add, immediately after the `display_name` property:

```json
"featured": {
  "type": "boolean",
  "description": "Include this model in the prerendered /costs/compare/<a>-vs-<b> page set. Not cleared on deprecation — see docs/superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md."
},
```

- [ ] **Step 2: Add the field to the shared TypeScript type**

In `web/lib/types.ts`, inside `CommonFields`, add after `display_name: string;`:

```ts
  featured?: boolean;
```

- [ ] **Step 3: Set `featured: true` on the 12 LLM rows**

Use the `Edit` tool on `costs/llm.json`. For each `model_id` below, insert `"featured": true,` on the line immediately after that row's `"display_name": ...` line:

`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`, `gpt-5.5`, `gpt-5.4-mini`, `gemini-2.5-pro`, `gemini-3.6-flash`, `deepseek-v4-pro`, `grok-4.5`, `llama-4-maverick`, `mistral-large-2512`, `qwen3.7-max`

- [ ] **Step 4: Set `featured: true` on the 6 STT rows**

Same procedure on `costs/stt.json` for: `nova-3-monolingual`, `universal-3-pro`, `whisper-large-v3-turbo`, `gpt-4o-transcribe`, `ink-2`, `solaria-3`

- [ ] **Step 5: Set `featured: true` on the 6 TTS rows**

Same procedure on `costs/tts.json` for: `eleven_flash_v2_5`, `eleven_v3`, `sonic-3.5`, `gpt-4o-mini-tts`, `aura-2`, `inworld-tts-2`

- [ ] **Step 6: Format and verify counts**

```bash
cd "$(git rev-parse --show-toplevel)"
pnpm exec prettier --write costs/llm.json costs/stt.json costs/tts.json
for f in llm stt tts; do
  echo -n "$f: "
  jq -r '[.models[] | select(.featured == true)] | length' costs/$f.json
done
```

Expected output:

```
llm: 12
stt: 6
tts: 6
```

- [ ] **Step 7: Validate against the schemas**

```bash
for f in llm stt tts; do
  printf "%-4s " "$f"
  /Users/r/.local/bin/check-jsonschema --schemafile costs/schema/$f.schema.json costs/$f.json 2>&1 | tail -1
done
```

Expected: three lines each ending `ok -- validation done`.

- [ ] **Step 8: Verify no other field changed**

```bash
git diff -U0 costs/ | grep -E '^[+-]\s+"' | sed -E 's/^([+-])\s+"([a-z_0-9]+)".*/\1\2/' | sort | uniq -c
```

Expected: only `+featured` lines, 24 of them. Any other field name in the output means a row was damaged — fix before committing.

- [ ] **Step 9: Commit**

```bash
git add costs/schema/llm.schema.json costs/schema/stt.schema.json costs/schema/tts.schema.json
git add costs/llm.json costs/stt.json costs/tts.json web/lib/types.ts
git commit -m "feat(costs): add featured flag for comparison pages (schema v2.3)"
```

---

## Task 2: CI guard and lockfile

**Files:**

- Create: `scripts/costs/check-featured.mjs`
- Create: `scripts/costs/check-featured.test.mjs`
- Create: `costs/featured.lock.json` (generated)
- Modify: `package.json`
- Modify: `.github/workflows/costs-validate.yml`

**Interfaces:**

- Consumes: `featured` from Task 1.
- Produces: `pairSlug(a, b) -> string`, `featuredIds(data) -> string[]`, `pairSlugs(ids) -> string[]`, `checkInvariants({ llm, stt, tts, lock }) -> string[]` (array of error messages; empty means pass). Also produces `costs/featured.lock.json` with shape `{ "slugs": string[] }`, which Task 3 imports.

- [ ] **Step 1: Write the failing test**

Create `scripts/costs/check-featured.test.mjs`:

```js
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  pairSlug,
  featuredIds,
  pairSlugs,
  checkInvariants,
} from "./check-featured.mjs";

test("pairSlug sorts ids deterministically", () => {
  assert.equal(
    pairSlug("gpt-5.5", "claude-opus-5"),
    "claude-opus-5-vs-gpt-5.5",
  );
  assert.equal(
    pairSlug("claude-opus-5", "gpt-5.5"),
    "claude-opus-5-vs-gpt-5.5",
  );
});

test("featuredIds selects only flagged rows", () => {
  const data = {
    models: [
      { model_id: "a", featured: true },
      { model_id: "b" },
      { model_id: "c", featured: false },
      { model_id: "d", featured: true },
    ],
  };
  assert.deepEqual(featuredIds(data), ["a", "d"]);
});

test("featuredIds keeps deprecated rows", () => {
  const data = {
    models: [{ model_id: "old", featured: true, deprecated_at: "2026-01-01" }],
  };
  assert.deepEqual(featuredIds(data), ["old"]);
});

test("pairSlugs produces every unordered pair, sorted", () => {
  assert.deepEqual(pairSlugs(["b", "a", "c"]), ["a-vs-b", "a-vs-c", "b-vs-c"]);
});

test("pairSlugs of two ids is one pair", () => {
  assert.deepEqual(pairSlugs(["x", "y"]), ["x-vs-y"]);
});

test("passes when lock matches and invariants hold", () => {
  const cat = (ids) => ({
    models: ids.map((id) => ({ model_id: id, featured: true })),
  });
  const errors = checkInvariants({
    llm: cat(["a", "b"]),
    stt: cat(["c", "d"]),
    tts: cat(["e", "f"]),
    lock: { slugs: ["a-vs-b", "c-vs-d", "e-vs-f"] },
  });
  assert.deepEqual(errors, []);
});

test("fails when a category has fewer than two featured models", () => {
  const cat = (ids) => ({
    models: ids.map((id) => ({ model_id: id, featured: true })),
  });
  const errors = checkInvariants({
    llm: cat(["a"]),
    stt: cat(["c", "d"]),
    tts: cat(["e", "f"]),
    lock: { slugs: ["c-vs-d", "e-vs-f"] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /llm has 1 featured model/);
});

test("fails when a locked slug disappears", () => {
  const cat = (ids) => ({
    models: ids.map((id) => ({ model_id: id, featured: true })),
  });
  const errors = checkInvariants({
    llm: cat(["a", "b"]),
    stt: cat(["c", "d"]),
    tts: cat(["e", "f"]),
    lock: { slugs: ["a-vs-b", "a-vs-z", "c-vs-d", "e-vs-f"] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /a-vs-z/);
  assert.match(errors[0], /no longer generated/);
});

test("fails when a featured replaced_by_model_id does not resolve", () => {
  const errors = checkInvariants({
    llm: {
      models: [
        { model_id: "a", featured: true, replaced_by_model_id: "ghost" },
        { model_id: "b", featured: true },
      ],
    },
    stt: {
      models: [
        { model_id: "c", featured: true },
        { model_id: "d", featured: true },
      ],
    },
    tts: {
      models: [
        { model_id: "e", featured: true },
        { model_id: "f", featured: true },
      ],
    },
    lock: { slugs: ["a-vs-b", "c-vs-d", "e-vs-f"] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /ghost/);
});

test("reports new slugs missing from the lock", () => {
  const cat = (ids) => ({
    models: ids.map((id) => ({ model_id: id, featured: true })),
  });
  const errors = checkInvariants({
    llm: cat(["a", "b", "c"]),
    stt: cat(["d", "e"]),
    tts: cat(["f", "g"]),
    lock: { slugs: ["a-vs-b", "d-vs-e", "f-vs-g"] },
  });
  assert.equal(errors.length, 1);
  assert.match(errors[0], /--write/);
});
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
node --test scripts/costs/check-featured.test.mjs
```

Expected: FAIL — `Cannot find module '.../scripts/costs/check-featured.mjs'`.

- [ ] **Step 3: Write the implementation**

Create `scripts/costs/check-featured.mjs`:

```js
import { readFile, writeFile } from "node:fs/promises";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(__dirname, "..", "..");
const DATA_DIR = join(REPO_ROOT, "costs");
const LOCK_PATH = join(DATA_DIR, "featured.lock.json");

const CATEGORIES = ["llm", "stt", "tts"];
const MIN_FEATURED_PER_CATEGORY = 2;

// Sorted with bare .sort() (code-unit order) rather than localeCompare, which is
// locale-dependent and would yield different route sets on different machines.
export function pairSlug(a, b) {
  return [a, b].sort().join("-vs-");
}

export function featuredIds(data) {
  return data.models.filter((m) => m.featured === true).map((m) => m.model_id);
}

export function pairSlugs(ids) {
  const sorted = [...ids].sort();
  const out = [];
  for (let i = 0; i < sorted.length; i++) {
    for (let j = i + 1; j < sorted.length; j++) {
      out.push(pairSlug(sorted[i], sorted[j]));
    }
  }
  return out.sort();
}

export function computeSlugs({ llm, stt, tts }) {
  return [
    ...pairSlugs(featuredIds(llm)),
    ...pairSlugs(featuredIds(stt)),
    ...pairSlugs(featuredIds(tts)),
  ].sort();
}

export function checkInvariants({ llm, stt, tts, lock }) {
  const data = { llm, stt, tts };
  const errors = [];

  for (const category of CATEGORIES) {
    const ids = featuredIds(data[category]);
    if (ids.length < MIN_FEATURED_PER_CATEGORY) {
      errors.push(
        `${category} has ${ids.length} featured model(s); at least ${MIN_FEATURED_PER_CATEGORY} are required or its comparison pages vanish`,
      );
    }
    const allIds = new Set(data[category].models.map((m) => m.model_id));
    for (const row of data[category].models) {
      if (row.featured !== true) continue;
      if (row.replaced_by_model_id && !allIds.has(row.replaced_by_model_id)) {
        errors.push(
          `${category}: featured model ${row.model_id} has replaced_by_model_id "${row.replaced_by_model_id}" which does not resolve in-file`,
        );
      }
    }
  }

  const computed = computeSlugs(data);
  const locked = [...(lock?.slugs ?? [])].sort();

  const removed = locked.filter((s) => !computed.includes(s));
  if (removed.length > 0) {
    errors.push(
      `these slugs are in costs/featured.lock.json but are no longer generated, so previously indexed pages would 404: ${removed.join(", ")}`,
    );
  }

  const added = computed.filter((s) => !locked.includes(s));
  if (added.length > 0) {
    errors.push(
      `${added.length} new slug(s) are not in the lockfile; run \`pnpm costs:featured --write\` and commit costs/featured.lock.json: ${added.join(", ")}`,
    );
  }

  return errors;
}

async function readCategory(name) {
  return JSON.parse(await readFile(join(DATA_DIR, `${name}.json`), "utf-8"));
}

async function main() {
  const write = process.argv.includes("--write");
  const [llm, stt, tts] = await Promise.all(CATEGORIES.map(readCategory));

  if (write) {
    const slugs = computeSlugs({ llm, stt, tts });
    await writeFile(
      LOCK_PATH,
      JSON.stringify({ slugs }, null, 2) + "\n",
      "utf-8",
    );
    console.log(`Wrote ${slugs.length} slug(s) to costs/featured.lock.json`);
    process.exit(0);
  }

  let lock;
  try {
    lock = JSON.parse(await readFile(LOCK_PATH, "utf-8"));
  } catch {
    console.error(
      "costs/featured.lock.json is missing. Run `pnpm costs:featured --write`.",
    );
    process.exit(1);
  }

  const errors = checkInvariants({ llm, stt, tts, lock });
  if (errors.length === 0) {
    console.log(`Featured set OK (${lock.slugs.length} comparison pages).`);
    process.exit(0);
  }

  for (const err of errors) console.error(`- ${err}`);
  process.exit(1);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(2);
  });
}
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
node --test scripts/costs/check-featured.test.mjs
```

Expected: `# pass 10`, `# fail 0`.

- [ ] **Step 5: Generate the lockfile**

```bash
node scripts/costs/check-featured.mjs --write
jq -r '.slugs | length' costs/featured.lock.json
```

Expected: `Wrote 96 slug(s) to costs/featured.lock.json`, then `96`.

- [ ] **Step 6: Verify the guard passes against real data**

```bash
node scripts/costs/check-featured.mjs
```

Expected: `Featured set OK (96 comparison pages).`

- [ ] **Step 7: Add the pnpm script**

In the root `package.json`, add after the `"costs:stale"` line:

```json
    "costs:featured": "node scripts/costs/check-featured.mjs",
```

- [ ] **Step 8: Wire both the guard and the previously-unrun unit tests into CI**

`scripts/costs/*.test.mjs` currently run nowhere. Append to `.github/workflows/costs-validate.yml`, after the existing "Cross-field validity checks" step:

```yaml
- uses: actions/setup-node@v4
  with:
    node-version: "22"
- name: Costs script unit tests
  run: node --test "scripts/costs/*.test.mjs"
- name: Featured set guard
  run: node scripts/costs/check-featured.mjs
```

Then extend the workflow's `on.pull_request.paths` list to include `scripts/costs/**` so the guard runs when the script itself changes:

```yaml
paths:
  - "costs/**"
  - "scripts/costs/**"
  - ".github/workflows/costs-validate.yml"
```

- [ ] **Step 9: Run the full script test files as CI will**

```bash
node --test "scripts/costs/*.test.mjs"
```

Expected: all tests pass across `check-stale.test.mjs`, `sync-telephony.test.mjs`, and `check-featured.test.mjs`. If a pre-existing test fails, stop and report it — do not fix unrelated tests in this task.

- [ ] **Step 10: Commit**

```bash
git add scripts/costs/check-featured.mjs scripts/costs/check-featured.test.mjs
git add costs/featured.lock.json package.json .github/workflows/costs-validate.yml
git commit -m "feat(costs): guard the featured comparison set in CI"
```

---

## Task 3: `web/lib/featured.ts`

**Files:**

- Create: `web/lib/featured.ts`

**Interfaces:**

- Consumes: `featured` from Task 1, `costs/featured.lock.json` from Task 2, and the existing `llm`, `stt`, `tts` exports from `web/lib/costs.ts`.
- Produces:
  - `type FeaturedCategory = 'llm' | 'stt' | 'tts'`
  - `type FeaturedPair = { slug: string; category: FeaturedCategory; models: [FeaturedRow, FeaturedRow] }`
  - `pairSlug(a: string, b: string): string`
  - `featuredPairs: FeaturedPair[]`
  - `pairBySlug: Map<string, FeaturedPair>`

  Tasks 4 and 6 both import from here.

- [ ] **Step 1: Write the module**

Create `web/lib/featured.ts`:

```ts
import { llm, stt, tts } from "./costs";
import lockJson from "../../costs/featured.lock.json";
import type { LLMRow, STTRow, TTSRow } from "./types";

export type FeaturedCategory = "llm" | "stt" | "tts";
export type FeaturedRow = LLMRow | STTRow | TTSRow;

export type FeaturedPair = {
  slug: string;
  category: FeaturedCategory;
  models: [FeaturedRow, FeaturedRow];
};

/**
 * Bare .sort() gives UTF-16 code-unit order. Do not switch to localeCompare —
 * it is locale-dependent and would generate a different route set per machine.
 * This must stay byte-identical to pairSlug() in scripts/costs/check-featured.mjs.
 */
export function pairSlug(a: string, b: string): string {
  return [a, b].sort().join("-vs-");
}

function featuredOf<T extends FeaturedRow>(rows: T[]): T[] {
  return rows
    .filter((m) => m.featured === true)
    .sort((a, b) =>
      a.model_id < b.model_id ? -1 : a.model_id > b.model_id ? 1 : 0,
    );
}

function pairsFor(
  category: FeaturedCategory,
  rows: FeaturedRow[],
): FeaturedPair[] {
  const out: FeaturedPair[] = [];
  for (let i = 0; i < rows.length; i++) {
    for (let j = i + 1; j < rows.length; j++) {
      out.push({
        slug: pairSlug(rows[i].model_id, rows[j].model_id),
        category,
        models: [rows[i], rows[j]],
      });
    }
  }
  return out;
}

export const featuredPairs: FeaturedPair[] = [
  ...pairsFor("llm", featuredOf(llm.models)),
  ...pairsFor("stt", featuredOf(stt.models)),
  ...pairsFor("tts", featuredOf(tts.models)),
];

export const pairBySlug: Map<string, FeaturedPair> = new Map(
  featuredPairs.map((p) => [p.slug, p]),
);

// Build-time contract with the CI guard. `allowJs: false` in tsconfig prevents
// importing scripts/costs/check-featured.mjs directly, so the lockfile is the
// shared artifact. A mismatch fails `pnpm site:build` rather than silently
// shipping a route set that CI believes is something else.
{
  const computed = featuredPairs.map((p) => p.slug).sort();
  const locked = [...lockJson.slugs].sort();
  const missing = computed.filter((s) => !locked.includes(s));
  const stale = locked.filter((s) => !computed.includes(s));
  if (missing.length > 0 || stale.length > 0) {
    throw new Error(
      "costs/featured.lock.json is out of date. Run `pnpm costs:featured --write`.\n" +
        `  missing from lock: ${missing.join(", ") || "(none)"}\n` +
        `  stale in lock:     ${stale.join(", ") || "(none)"}`,
    );
  }
}
```

- [ ] **Step 2: Verify it type-checks and the assertion is satisfied**

```bash
cd web && pnpm exec tsc --noEmit
```

Expected: no output (success). If `lockJson.slugs` errors as `never[]`, confirm `resolveJsonModule: true` is set in `web/tsconfig.json` — it is.

- [ ] **Step 3: Prove the build-time assertion actually fires**

Temporarily corrupt the lockfile and confirm the guard catches it:

```bash
cd "$(git rev-parse --show-toplevel)"
cp costs/featured.lock.json /tmp/featured.lock.bak
jq '.slugs |= (. + ["fake-vs-slug"])' costs/featured.lock.json > /tmp/lock.tmp && mv /tmp/lock.tmp costs/featured.lock.json
pnpm site:build 2>&1 | grep -c "featured.lock.json is out of date"
cp /tmp/featured.lock.bak costs/featured.lock.json
```

Expected: a non-zero count from `grep -c`, then the file is restored. Confirm with `node scripts/costs/check-featured.mjs` printing `Featured set OK (96 comparison pages).` before moving on.

- [ ] **Step 4: Commit**

```bash
git add web/lib/featured.ts
git commit -m "feat(web): derive featured comparison pairs from costs data"
```

---

## Task 4: Static `[pair]` comparison pages

**Files:**

- Create: `web/app/(dispatch)/compare/[pair]/page.tsx`
- Create: `web/components/deprecation-notice.tsx`
- Modify: `web/components/compare-table.tsx` (add a `removable` prop — see Step 0)

**Interfaces:**

- Consumes: `featuredPairs`, `pairBySlug`, `FeaturedPair` from Task 3; the existing `LLMCompareTable`, `STTCompareTable`, `TTSCompareTable` from `web/components/compare-table.tsx`.
- Produces: 96 static routes at `/costs/compare/<slug>`, and a `removable?: boolean` prop on all three compare-table components (defaults to `true`, preserving current behavior for Task 5's picker).

- [ ] **Step 0: Suppress the remove-links on static pages**

`CompareGrid` renders a remove-link per model at `web/components/compare-table.tsx:35-42`:

```tsx
<a href={compareHrefRemove(currentIds, m.model_id)} className="compare-remove" rel="nofollow" ...>
```

On a static pair page that link is both meaningless (the pair is fixed) and harmful: 96 pages × 2 models would emit ~192 crawlable `?m=` hrefs pointing straight back into the query space this whole branch exists to delist.

Add an optional `removable` prop, defaulting to `true` so Task 5's picker is unaffected.

In `CompareGrid`, extend the signature:

```tsx
function CompareGrid({
  models,
  currentIds,
  rows,
  removable = true,
}: {
  models: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  rows: CompareRow[];
  removable?: boolean;
}) {
```

Wrap the remove-link so it renders only when `removable` is true:

```tsx
{
  removable && (
    <a
      href={compareHrefRemove(currentIds, m.model_id)}
      className="compare-remove"
      rel="nofollow"
      title={`Remove ${m.display_name}`}
      aria-label={`Remove ${m.display_name} from comparison`}
    >
      ×
    </a>
  );
}
```

Then thread the prop through all three exported wrappers. Each currently reads:

```tsx
export function LLMCompareTable({ models, currentIds }: { models: LLMRow[]; currentIds: string[] }) {
```

Change each to accept and forward it — `LLMCompareTable` with `LLMRow[]`, `STTCompareTable` with `STTRow[]`, `TTSCompareTable` with `TTSRow[]`:

```tsx
export function LLMCompareTable({
  models,
  currentIds,
  removable = true,
}: {
  models: LLMRow[];
  currentIds: string[];
  removable?: boolean;
}) {
```

and pass `removable` down in each wrapper's `<CompareGrid ... />` call.

The `[pair]` page in Step 2 passes `removable={false}`.

- [ ] **Step 1: Write the deprecation notice component**

Create `web/components/deprecation-notice.tsx`:

```tsx
import type { FeaturedRow } from "@/lib/featured";

export function DeprecationNotice({
  models,
  successorSlug,
}: {
  models: FeaturedRow[];
  successorSlug: string | null;
}) {
  const deprecated = models.filter((m) => m.deprecated_at);
  if (deprecated.length === 0) return null;

  return (
    <div
      style={{
        border: "2px solid var(--color-ink)",
        background: "var(--color-paper)",
        padding: "14px 18px",
        marginBottom: 24,
        fontFamily: "var(--font-mono)",
        fontSize: 12,
      }}
      role="note"
    >
      {deprecated.map((m) => (
        <div key={m.model_id}>
          <b>{m.display_name}</b> was deprecated on {m.deprecated_at}.
          {m.replaced_by_model_id ? (
            <>
              {" "}
              Replaced by <code>{m.replaced_by_model_id}</code>.
            </>
          ) : null}
        </div>
      ))}
      {successorSlug ? (
        <div style={{ marginTop: 8 }}>
          <a href={`/costs/compare/${successorSlug}`}>
            See the current comparison →
          </a>
        </div>
      ) : null}
    </div>
  );
}
```

- [ ] **Step 2: Write the route**

Create `web/app/(dispatch)/compare/[pair]/page.tsx`:

```tsx
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { featuredPairs, pairBySlug, pairSlug } from "@/lib/featured";
import type { FeaturedPair } from "@/lib/featured";
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from "@/components/compare-table";
import { DeprecationNotice } from "@/components/deprecation-notice";
import { SITE_ORIGIN } from "@/lib/url";
import type { LLMRow, STTRow, TTSRow } from "@/lib/types";

export const dynamic = "force-static";
export const dynamicParams = false;

export function generateStaticParams() {
  return featuredPairs.map((p) => ({ pair: p.slug }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ pair: string }>;
}): Promise<Metadata> {
  const { pair } = await params;
  const entry = pairBySlug.get(pair);
  if (!entry) return {};
  const [a, b] = entry.models;
  return {
    title: `${a.display_name} vs ${b.display_name} — cost comparison`,
    description: `Side-by-side pricing and capabilities for ${a.display_name} (${a.provider}) and ${b.display_name} (${b.provider}). Schema-validated, refreshed weekly.`,
    alternates: {
      canonical: new URL(`/costs/compare/${pair}`, SITE_ORIGIN).toString(),
    },
  };
}

function successorSlugFor(entry: FeaturedPair): string | null {
  const [a, b] = entry.models;
  if (!a.deprecated_at && !b.deprecated_at) return null;

  // A deprecated model contributes its successor; a live one contributes itself.
  const currentA = a.deprecated_at ? a.replaced_by_model_id : a.model_id;
  const currentB = b.deprecated_at ? b.replaced_by_model_id : b.model_id;
  if (!currentA || !currentB) return null;
  if (currentA === a.model_id && currentB === b.model_id) return null;

  const candidate = pairSlug(currentA, currentB);
  return pairBySlug.has(candidate) ? candidate : null;
}

export default async function PairPage({
  params,
}: {
  params: Promise<{ pair: string }>;
}) {
  const { pair } = await params;
  const entry = pairBySlug.get(pair);
  if (!entry) notFound();

  const [a, b] = entry.models;
  const currentIds = [a.model_id, b.model_id];
  const today = new Date().toISOString().slice(0, 10);

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} ·
            COMPARE
          </div>
          <div className="right">
            FILE: <b>{entry.category.toUpperCase()}</b> · 2 models
          </div>
        </div>
      </div>

      <header
        style={{
          padding: "40px 0 28px",
          borderBottom: "2px solid var(--color-ink)",
        }}
      >
        <div className="wrap">
          <h1 className="dispatch-h1">
            {a.display_name} <em className="it">vs</em> {b.display_name}
          </h1>
        </div>
      </header>

      <div className="toolbar">
        <div className="wrap row">
          <a href="/costs" className="btn btn-outline">
            ← all costs
          </a>
          <a href="/costs/compare" className="btn btn-outline">
            build your own
          </a>
        </div>
      </div>

      <section className="cat">
        <div className="wrap">
          <DeprecationNotice
            models={entry.models}
            successorSlug={successorSlugFor(entry)}
          />
          {entry.category === "llm" && (
            <LLMCompareTable
              models={entry.models as LLMRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
          {entry.category === "stt" && (
            <STTCompareTable
              models={entry.models as STTRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
          {entry.category === "tts" && (
            <TTSCompareTable
              models={entry.models as TTSRow[]}
              currentIds={currentIds}
              removable={false}
            />
          )}
        </div>
      </section>
    </>
  );
}
```

- [ ] **Step 3: Build and confirm 96 pages prerender**

```bash
cd "$(git rev-parse --show-toplevel)"
pnpm site:build 2>&1 | tail -30
```

Expected: the route table lists `● /compare/[pair]` with `96 paths` (shown as a few entries plus `[+N more paths]`), and the build succeeds. If `/compare/[pair]` appears as `ƒ (Dynamic)`, the `force-static` / `dynamicParams` exports are missing or misspelled.

- [ ] **Step 4: Verify a rendered page has real content**

```bash
cd "$(git rev-parse --show-toplevel)"
PAIR_DIR=$(dirname "$(find web/.next/server/app -name 'claude-opus-5-vs-*.html' | head -1)")
echo "pair dir: $PAIR_DIR"
ls "$PAIR_DIR"/*.html | wc -l
grep -l "claude-opus-5" "$PAIR_DIR"/*.html | head -3
```

Expected: a directory containing **96** prerendered HTML files, and at least one matching the grep.

- [ ] **Step 5: Assert the pair pages emit no `?m=` links**

This is the check that proves Step 0 worked. Without `removable={false}`, each page emits one `?m=` remove-link per model — ~192 crawlable links back into the query space this branch exists to delist.

```bash
cd "$(git rev-parse --show-toplevel)"
PAIR_DIR=$(dirname "$(find web/.next/server/app -name 'claude-opus-5-vs-*.html' | head -1)")
echo -n "pages containing a ?m= href (expect 0): "
grep -l '?m=' "$PAIR_DIR"/*.html | wc -l
```

Expected: `0`. Any non-zero count means `removable={false}` is not reaching `CompareGrid` — fix it before committing.

- [ ] **Step 6: Commit**

```bash
git add "web/app/(dispatch)/compare/[pair]/page.tsx" web/components/deprecation-notice.tsx web/components/compare-table.tsx
git commit -m "feat(web): prerender featured model comparison pages"
```

---

## Task 5: Make `/costs/compare` static

**Files:**

- Create: `web/components/compare-picker.tsx`
- Modify: `web/app/(dispatch)/compare/page.tsx` (full rewrite)
- Modify: `web/components/compare-link.tsx:5` (remove `rel="nofollow"`, keep the href)

**Interfaces:**

- Consumes: `llm`, `stt`, `tts` from `web/lib/costs.ts`; `MAX_COMPARE`, `compareHref` from `web/lib/url.ts`.
- Produces: a `<CompareModels llm={...} stt={...} tts={...} />` client component. No later task depends on it.

- [ ] **Step 1: Write the client picker**

Create `web/components/compare-picker.tsx`. Note `useSearchParams()` requires a `<Suspense>` boundary, supplied by the parent in Step 2.

```tsx
"use client";

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from "./compare-table";
import { MAX_COMPARE, compareHref } from "@/lib/url";
import type { LLMRow, STTRow, TTSRow } from "@/lib/types";

type Props = { llm: LLMRow[]; stt: STTRow[]; tts: TTSRow[] };

function parseIds(raw: string | null): string[] {
  return (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, MAX_COMPARE);
}

export function CompareModels({ llm, stt, tts }: Props) {
  const searchParams = useSearchParams();
  const [ids, setIds] = useState<string[]>(() =>
    parseIds(searchParams.get("m")),
  );

  // Keep the URL shareable without triggering a navigation or a server render.
  useEffect(() => {
    const href = compareHref(ids);
    if (window.location.pathname + window.location.search !== href) {
      window.history.replaceState(null, "", href);
    }
  }, [ids]);

  const add = useCallback((id: string) => {
    setIds((prev) =>
      prev.includes(id) || prev.length >= MAX_COMPARE ? prev : [...prev, id],
    );
  }, []);

  const clear = useCallback(() => setIds([]), []);

  const selectedLLM = ids
    .map((id) => llm.find((m) => m.model_id === id))
    .filter(Boolean) as LLMRow[];
  const selectedSTT = ids
    .map((id) => stt.find((m) => m.model_id === id))
    .filter(Boolean) as STTRow[];
  const selectedTTS = ids
    .map((id) => tts.find((m) => m.model_id === id))
    .filter(Boolean) as TTSRow[];
  const total = selectedLLM.length + selectedSTT.length + selectedTTS.length;
  const currentIds = [...selectedLLM, ...selectedSTT, ...selectedTTS].map(
    (m) => m.model_id,
  );

  return (
    <>
      <div className="toolbar">
        <div className="wrap row">
          <a href="/costs" className="btn btn-outline">
            ← all costs
          </a>
          {total > 0 && (
            <button type="button" className="btn btn-outline" onClick={clear}>
              clear
            </button>
          )}
          <div
            style={{
              marginLeft: "auto",
              fontFamily: "var(--font-mono)",
              fontSize: 11,
            }}
          >
            {total} of {MAX_COMPARE} slots
          </div>
        </div>
      </div>

      <section className="cat">
        <div className="wrap">
          {selectedLLM.length > 0 && (
            <LLMCompareTable models={selectedLLM} currentIds={currentIds} />
          )}
          {selectedSTT.length > 0 && (
            <STTCompareTable models={selectedSTT} currentIds={currentIds} />
          )}
          {selectedTTS.length > 0 && (
            <TTSCompareTable models={selectedTTS} currentIds={currentIds} />
          )}

          <h3
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: "var(--color-mute)",
              margin: "24px 0 16px",
            }}
          >
            {total === 0 ? "Available models" : "Add another model"}
          </h3>
          <ModelGroup
            label="LLMs"
            models={llm}
            currentIds={currentIds}
            onAdd={add}
          />
          <ModelGroup
            label="Speech-to-Text"
            models={stt}
            currentIds={currentIds}
            onAdd={add}
          />
          <ModelGroup
            label="Text-to-Speech"
            models={tts}
            currentIds={currentIds}
            onAdd={add}
          />
        </div>
      </section>
    </>
  );
}

function ModelGroup({
  label,
  models,
  currentIds,
  onAdd,
}: {
  label: string;
  models: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  onAdd: (id: string) => void;
}) {
  const available = models.filter((m) => !currentIds.includes(m.model_id));
  if (available.length === 0) return null;
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: "var(--color-mute)",
          marginBottom: 8,
        }}
      >
        {label}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {available.map((m) => (
          <button
            key={m.model_id}
            type="button"
            className="add-pill"
            onClick={() => onAdd(m.model_id)}
          >
            <span className="add-pill-plus">+</span>
            <span>
              <span className="add-pill-prov">{m.provider}</span>{" "}
              {m.display_name}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Rewrite the page as a static shell**

Replace the entire contents of `web/app/(dispatch)/compare/page.tsx` with:

```tsx
import { Suspense } from "react";
import { llm, stt, tts } from "@/lib/costs";
import { CompareModels } from "@/components/compare-picker";
import { featuredPairs } from "@/lib/featured";

export const dynamic = "force-static";

export const metadata = {
  title: "Compare model costs — Hail",
  description:
    "Compare AI model providers side-by-side. Schema-validated, refreshed weekly.",
};

export default function ComparePage() {
  const today = new Date().toISOString().slice(0, 10);

  return (
    <>
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> HAIL.SO / DISPATCH · {today} ·
            COMPARE
          </div>
        </div>
      </div>

      <header
        style={{
          padding: "40px 0 28px",
          borderBottom: "2px solid var(--color-ink)",
        }}
      >
        <div className="wrap">
          <h1 className="dispatch-h1">
            <em className="it">side</em> by side.
          </h1>
        </div>
      </header>

      <Suspense fallback={null}>
        <CompareModels llm={llm.models} stt={stt.models} tts={tts.models} />
      </Suspense>

      <section
        style={{ padding: "32px 0", borderTop: "2px solid var(--color-ink)" }}
      >
        <div className="wrap">
          <h2
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: "var(--color-mute)",
              margin: "0 0 16px",
            }}
          >
            Popular comparisons
          </h2>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {featuredPairs.map((p) => (
              <a
                key={p.slug}
                className="add-pill"
                href={`/costs/compare/${p.slug}`}
              >
                {p.models[0].display_name} vs {p.models[1].display_name}
              </a>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
```

The "Popular comparisons" block is deliberate: it gives crawlers a bounded set of 96 real links in place of the unbounded `?m=` fan-out, so the page still has crawlable outbound value.

- [ ] **Step 3: Remove the now-pointless `rel="nofollow"` from the compare link**

In `web/components/compare-link.tsx`, delete the line `rel="nofollow"`. The link points at `/costs/compare?m=<id>`, which is now a static shell — there is nothing to protect against, and `nofollow` only suppresses legitimate crawl signal.

- [ ] **Step 4: Build and confirm `/compare` is no longer dynamic**

```bash
cd "$(git rev-parse --show-toplevel)"
pnpm site:build 2>&1 | grep -E "^[├└─\s]*[○●ƒ]\s|Route \(app\)"
```

Expected: `○ /compare` (Static) or `● /compare`. **A `ƒ /compare` line means this task failed** — that symbol is the entire bug being fixed.

- [ ] **Step 5: Assert the prerendered shell carries the bounded link set**

The page must ship as static HTML containing exactly the 96 "Popular comparisons" links and **zero** `?m=` hrefs — that combination is the whole fix, so assert it against the build output rather than eyeballing a page.

```bash
cd "$(git rev-parse --show-toplevel)"
SHELL_HTML=$(find web/.next/server/app -name 'compare.html' | head -1)
echo "shell: $SHELL_HTML"
echo -n "pair links (expect 96): "
grep -o 'href="/costs/compare/[^"]*"' "$SHELL_HTML" | sort -u | wc -l
echo -n "crawlable ?m= hrefs (expect 0): "
grep -o 'href="[^"]*?m=[^"]*"' "$SHELL_HTML" | wc -l
```

Expected: `96` and `0`. A non-zero `?m=` count means an `<a href>` survived the conversion to `<button>` and the fan-out is still crawlable.

Then confirm an old link still resolves to that shell rather than 404ing:

```bash
pnpm site:start &
sleep 4
curl -s -o /dev/null -w "%{http_code}\n" "http://localhost:3000/costs/compare?m=claude-opus-5,gpt-5.5"
kill %1
```

Expected: `200`. Client-side selection cannot be asserted from `curl`; the build assertions above plus this status code are the gate for this task.

- [ ] **Step 6: Commit**

```bash
git add web/components/compare-picker.tsx "web/app/(dispatch)/compare/page.tsx" web/components/compare-link.tsx
git commit -m "feat(web): make /costs/compare static with client-side selection"
```

---

## Task 6: Sitemap and canonical-host redirect

**Files:**

- Create: `web/app/sitemap.ts`
- Modify: `web/next.config.ts`

**Interfaces:**

- Consumes: `featuredPairs` from Task 3, `SITE_ORIGIN` from `web/lib/url.ts`.
- Produces: `/costs/sitemap.xml`, and a 308 from the raw Vercel host.

- [ ] **Step 1: Write the sitemap**

Create `web/app/sitemap.ts`:

```ts
import type { MetadataRoute } from "next";
import { featuredPairs } from "@/lib/featured";
import { SITE_ORIGIN } from "@/lib/url";

export const dynamic = "force-static";

const abs = (path: string) => new URL(path, SITE_ORIGIN).toString();

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: abs("/costs"), changeFrequency: "weekly", priority: 1 },
    { url: abs("/costs/compare"), changeFrequency: "weekly", priority: 0.8 },
    ...featuredPairs.map((p) => ({
      url: abs(`/costs/compare/${p.slug}`),
      changeFrequency: "weekly" as const,
      priority: 0.6,
    })),
  ];
}
```

- [ ] **Step 2: Add the canonical-host redirect**

Replace `web/next.config.ts` with:

```ts
import type { NextConfig } from "next";

// The raw deployment host was serving the crawl trap directly, bypassing the
// hail.so rewrite. Redirecting it also removes the duplicate-content problem.
const VERCEL_HOST = "hail-costs.vercel.app";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  basePath: "/costs",
  async redirects() {
    return [
      {
        source: "/costs/:path*",
        has: [{ type: "host", value: VERCEL_HOST }],
        destination: "https://hail.so/costs/:path*",
        permanent: true,
        basePath: false,
      },
    ];
  },
};

export default nextConfig;
```

`basePath: false` and the `/costs/` prefix on `source` are both load-bearing, and they work together:

- **With** `basePath: false`, Next does **not** prefix `source`, so `source` must match the **raw incoming path** — which for this app already begins with `/costs`. Hence `source: "/costs/:path*"`.
- Writing `source: "/:path*"` instead captures `:path*` as `costs/compare`, producing the doubled destination `https://hail.so/costs/costs/compare`.
- Dropping `basePath: false` makes Next prefix `source` itself, so the rule only matches `/costs/costs/...` and silently never fires.

All three failure modes build cleanly and produce no warning, so **Step 4's `curl` check is the only thing that catches them.** Confirm the redirect target has exactly one `/costs` and that the query string survives.

- [ ] **Step 3: Build and verify the sitemap contains 98 URLs**

```bash
cd "$(git rev-parse --show-toplevel)"
pnpm site:build 2>&1 | grep -E "sitemap|Error"
pnpm site:start &
sleep 4
curl -s http://localhost:3000/costs/sitemap.xml | grep -c "<loc>"
curl -s http://localhost:3000/costs/sitemap.xml | grep -o "<loc>[^<]*</loc>" | head -3
kill %1
```

Expected: `98` (the index, the compare shell, and 96 pairs), and the first URLs absolute under `https://hail.so/costs`.

- [ ] **Step 4: Verify the redirect does not fire for normal hosts**

```bash
cd "$(git rev-parse --show-toplevel)"
pnpm site:start &
sleep 4
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/costs
curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" -H "Host: hail-costs.vercel.app" http://localhost:3000/costs
kill %1
```

Expected: `200` for the first, and `308 -> https://hail.so/costs` for the second. If the second returns 200, `basePath: false` is missing or the `has` host value is wrong.

- [ ] **Step 5: Commit**

```bash
git add web/app/sitemap.ts web/next.config.ts
git commit -m "feat(web): add costs sitemap and canonical-host redirect"
```

---

## Task 7: Runbook maintenance procedure

**Files:**

- Modify: `docs/operations/refresh-costs.md`

**Interfaces:**

- Consumes: everything above.
- Produces: documentation only.

- [ ] **Step 1: Add `featured` to the three "Per-row data model" blocks**

In the LLM, STT, and TTS blocks under "Per-row data model (cheat sheet)", add `featured` to each `Optional` list. For LLM, extend the `Optional (v2.2)` line to a new line:

```
Optional (v2.3): featured.
```

Add the same `Optional (v2.3): featured.` line to the STT and TTS blocks.

- [ ] **Step 2: Add `featured` to the three "Field order" blocks**

In each of the LLM, STT, and TTS field-order blocks, insert `featured (when true),` immediately after the line containing `display_name`. For LLM that line reads `provider, provider_url, model_id, display_name, model_family,` — insert the new entry on the following line so the block begins:

```
provider, provider_url, model_id, display_name, featured (when true),
model_family, knowledge_cutoff, aliases (when present),
```

For STT and TTS, whose blocks begin `provider, provider_url, model_id, display_name,`, insert `featured (when true),` as the second line.

- [ ] **Step 3: Add a "Featured models" section**

Insert this section immediately before "## Provider catalog":

````markdown
## Featured models

`featured: true` puts a model into the prerendered `/costs/compare/<a>-vs-<b>` page set. Every within-category pair of featured models becomes a static page. Design rationale: [`docs/superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md`](../superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md).

Rules for a refresh pass:

- **New marquee model launched?** It is a candidate for `featured: true`. Adding the flag creates its comparison pages on the next deploy — nothing else to edit.
- **Featured model deprecated?** **Keep the flag.** The page persists with a deprecation banner so an indexed URL never 404s. Make sure `replaced_by_model_id` resolves to a model in the same file.
- **Never drop a featured flag** to tidy up. Removing one deletes indexed pages; `scripts/costs/check-featured.mjs` will fail the build if you do.
- Adding or removing a flag changes the generated slug set, so regenerate and commit the lockfile:

```bash
pnpm costs:featured --write   # regenerates costs/featured.lock.json
pnpm costs:featured           # verifies invariants; must print "Featured set OK"
```

Keep the set small. Every model added creates a page against each existing featured model in its category, so N models produce N×(N−1)/2 pages.
````

- [ ] **Step 4: Add closing-checklist lines**

In the "## Closing checklist" section, add after the `pnpm site:build` line:

```markdown
- [ ] `pnpm costs:featured` passes (`Featured set OK`); if any `featured` flag changed, `costs/featured.lock.json` was regenerated with `--write` and staged
- [ ] `node --test "scripts/costs/*.test.mjs"` passes
```

- [ ] **Step 5: Add `featured` to the "When this runbook needs updates" trigger list**

Under "## When this runbook needs updates", the existing bullet about new schema fields already covers this. Add one bullet:

```markdown
- The featured-model policy changes (see "Featured models") — update that section and the closing checklist together
```

- [ ] **Step 6: Verify the spec link resolves**

```bash
cd docs/operations && test -e ../superpowers/specs/2026-07-27-costs-compare-crawl-trap-design.md && echo OK
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```bash
git add docs/operations/refresh-costs.md
git commit -m "docs(costs): document featured-model maintenance in the refresh runbook"
```

---

## Final verification

Run the whole gate before opening the PR:

```bash
cd "$(git rev-parse --show-toplevel)"
for f in llm stt tts; do
  /Users/r/.local/bin/check-jsonschema --schemafile costs/schema/$f.schema.json costs/$f.json
done
node --test "scripts/costs/*.test.mjs"
node scripts/costs/check-featured.mjs
pnpm exec prettier --check costs/*.json
pnpm site:build
```

Every command must pass, and the build's route table must show `● /compare/[pair]` with 96 paths and **no `ƒ` marker on `/compare`**.

## Out of scope — hand back to the operator

Two edits live in the **hail-website** repo and cannot be made from here. Report them in the PR description:

1. `robots.txt`: add `Disallow: /costs/compare?` (prefix match — leaves the bare path and `/costs/compare/<pair>` allowed) and `Sitemap: https://hail.so/costs/sitemap.xml`.
2. `sitemap.xml`: the bare `/costs/compare` entry is now superseded by `/costs/sitemap.xml`.
