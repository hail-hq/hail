# Costs compare: closing the crawl trap

**Status:** design
**Date:** 2026-07-27
**Author:** r13i

## Problem

`/costs/compare` served **642K requests in 12 hours** — ~15 req/s sustained, 10.89 GB egress, 3h of active CPU. Over the same window `/costs` served **12**.

That asymmetry is the tell. `/costs` is `force-static` and absorbed by the CDN; `/costs/compare` reads `searchParams`, so Next marks it dynamic and every distinct query string reaches a function.

Measured directly:

```console
$ curl -sI 'https://hail.so/costs/compare?m=gpt-4o' | grep -iE 'cache'
cache-control: private, no-cache, no-store, max-age=0, must-revalidate
x-vercel-cache: MISS

$ curl -sI 'https://hail.so/costs' | grep -iE 'cache|prerender'
cache-control: public, max-age=0, must-revalidate
x-nextjs-prerender: 1
x-vercel-cache: HIT
```

`pnpm site:build` says the same thing in one line:

```
├ ○ /                    (Static)
├ ƒ /compare             (Dynamic)  server-rendered on demand
└ ● /schema/[name]       (SSG)
```

### Why it never terminates

Three properties compound:

1. **The URL space is unbounded in practice.** `MAX_COMPARE = 6` ([`web/lib/url.ts:6`](../../../web/lib/url.ts)) over 148 model rows, and `?m=a,b` is treated as distinct from `?m=b,a`. That is ~9.5 × 10<sup>12</sup> ordered URLs.
2. **Every page advertises ~140 more.** [`web/app/(dispatch)/compare/page.tsx:279`](<../../../web/app/(dispatch)/compare/page.tsx>) renders an `add-pill` `<a href>` per unselected model, each pointing at a fresh URL. `rel="nofollow"` is advisory and widely ignored.
3. **`/costs/compare` is the advertised entry point.** `hail.so/robots.txt` is `User-agent: * / Allow: /` with no exclusions, and `hail.so/sitemap.xml` lists `/costs/compare` explicitly among its 25 URLs.

A crawler lands on the entry point, finds 140 links, follows them, finds 140 more each. Nothing bounds the walk.

### What this is not

It is not monetizable demand, and a paywall (Stripe machine payments, x402, or otherwise) would be the wrong instrument. Observed traffic hits `hail-costs.vercel.app` directly rather than the canonical `hail.so`, shows no diurnal curve across 12 hours, and reaches `/costs/compare` 53,500× more often than the `/costs` page that links to it. Those are scraper properties. A 402 would not convert them into buyers; it would only wall off the surface that earns Hail citations in AI answers (tenet 6, agent-first docs).

## Goals

1. Eliminate the function invocations and the egress.
2. Keep — and improve — the ability of search engines and AI answer engines to cite Hail comparison data.
3. Make the page set derive from `costs/*.json`, so a [refresh run](../../operations/refresh-costs.md) that adds a model can create comparison pages without a second list to maintain.
4. Keep existing `?m=` links working.

## Non-goals

- Any paid or metered access to costs data.
- Blocking AI crawlers wholesale. Well-behaved indexing of a bounded page set is the outcome we want.
- Restructuring the costs dataset schema beyond one additive optional field.

## Design

### 1. Static comparison pages

New route `web/app/(dispatch)/compare/[pair]/page.tsx`:

```ts
export const dynamic = "force-static";
export const dynamicParams = false; // unknown slug -> static 404, no function
export async function generateStaticParams() {
  /* from lib/featured.ts */
}
```

- **Slug**: the two `model_id`s sorted lexicographically, joined by `-vs-` — `/costs/compare/claude-sonnet-5-vs-gpt-5.5`.
- **No slug parsing.** `generateStaticParams` emits the slugs; `lib/featured.ts` exports a `Map<string, [Model, Model]>` for lookup. Model ids containing `-`, `_`, or `.` (`eleven_flash_v2_5`, `gemini-2.5-pro`) are therefore safe.
- **Flat namespace is safe.** Verified no `model_id` is shared across `llm.json`, `stt.json`, and `tts.json`, so category is inferable from the pair and needs no path segment.
- **Metadata**: `generateMetadata()` sets a per-page title (`claude-sonnet-5 vs gpt-5.5 — cost comparison`) and `alternates.canonical` pinned to the `hail.so` origin.
- **Body** reuses `LLMCompareTable` / `STTCompareTable` / `TTSCompareTable` from [`web/components/compare-table.tsx`](../../../web/components/compare-table.tsx) unchanged.

Pairs are within-category only. Comparing an LLM against a TTS model has no shared columns.

### 2. The `featured` flag

The page set is derived from the dataset, not from a parallel list.

- **Schema v2.3**: add `"featured": { "type": "boolean" }` to `costs/schema/{llm,stt,tts}.schema.json`. This is mandatory, not cosmetic: `models.items` is a `$ref` to `$defs.model`, which sets `additionalProperties: false`, so an undeclared `featured` would fail validation on every flagged row.
- **Field order**: `featured` sits immediately after `display_name` in all three categories. [`docs/operations/refresh-costs.md`](../../operations/refresh-costs.md) rule 6 makes field order non-negotiable, so the runbook's three field-order blocks must be updated in the same change.
- **Effect**: a refresh run that adds `"featured": true` to a new row creates that model's comparison pages on the next deploy. Nothing else to touch.

Initial set — 12 LLM, 6 STT, 6 TTS → **66 + 15 + 15 = 96 pages**:

| Category | Featured `model_id`s                                                                                                                                                                                                     |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| LLM      | `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`, `gpt-5.5`, `gpt-5.4-mini`, `gemini-2.5-pro`, `gemini-3.6-flash`, `deepseek-v4-pro`, `grok-4.5`, `llama-4-maverick`, `mistral-large-2512`, `qwen3.7-max` |
| STT      | `nova-3-monolingual`, `universal-3-pro`, `whisper-large-v3-turbo`, `gpt-4o-transcribe`, `ink-2`, `solaria-3`                                                                                                             |
| TTS      | `eleven_flash_v2_5`, `eleven_v3`, `sonic-3.5`, `gpt-4o-mini-tts`, `aura-2`, `inworld-tts-2`                                                                                                                              |

Selection notes: `claude-opus-5` takes the Opus slot over `claude-opus-4-8` — same $5/$25 pricing, and it is the current flagship as of its 2026-06-09 launch. `gemini-3.6-flash` is likewise the current flagship Flash as of the 2026-07-27 refresh. `sonic-3.5` is the only Sonic variant without a sunset notice — `sonic-3`, `sonic-2`, and `sonic-turbo` all retire 2026-10-20.

This list is the initial seed, not a fixed set. It is expected to churn as the runbook adds and retires models; the CI guard in part 6 is what makes that churn safe.

### 3. Deprecation handling

`generateStaticParams` includes a pair when both models are `featured`, **regardless of `deprecated_at`**. The flag is never cleared on deprecation.

Rationale: 26 rows already carry `deprecated_at` (20 LLM, 5 STT, 1 TTS) and are retained in the dataset. Dropping their pages would discard accumulated rankings every time a model retires, and "is `gpt-5.4` still available?" is a question the page can answer.

A deprecated model's page renders a banner (`gpt-5.4 was deprecated on 2026-03-01`) and, when `replaced_by_model_id` resolves to another featured model, links to the successor's comparison.

### 4. `/costs/compare` stops being dynamic

- The server component drops `searchParams` and gains `export const dynamic = 'force-static'`.
- A new client component `web/components/compare-picker.tsx` reads `useSearchParams()` inside a `<Suspense>` boundary, holds selection state, and writes back via `history.replaceState`. Existing `?m=a,b,c` links and bookmarks keep working and stay shareable.
- **`add-pill` `<a href>` becomes `<button>`.** This is the change that removes the 140-link fan-out; `rel="nofollow"` was never going to hold.

Result: the route prerenders to a static asset and serves from the edge for any query string. Function invocations go to zero.

### 5. Stopping the scraper

A static page still costs egress if a bot keeps fetching it, so caching alone is insufficient.

**In this repo:**

- **Canonical-host redirect** in [`web/next.config.ts`](../../../web/next.config.ts): a 308 from `hail-costs.vercel.app/costs/:path*` to `https://hail.so/costs/:path*`, scoped to that exact host so preview deployments are unaffected. Observed traffic targets the raw Vercel host, so this is the most direct lever, and it removes the duplicate-content problem at the same time.
- **`web/app/sitemap.ts`** → served at `/costs/sitemap.xml` given `basePath: '/costs'`, listing `/costs`, `/costs/compare`, and all 96 pair pages.

**In hail-website (separate repo — cannot be changed from here):**

- `robots.txt`: add `Disallow: /costs/compare?` (prefix match, so the bare path and the `/costs/compare/<pair>` pages stay allowed) and `Sitemap: https://hail.so/costs/sitemap.xml`.
- `sitemap.xml`: the bare `/costs/compare` entry is superseded by the new per-pair sitemap.

### 6. CI enforcement

`scripts/costs/check-featured.mjs` plus `check-featured.test.mjs`, following the existing [`check-stale.mjs`](../../../scripts/costs/check-stale.mjs) pattern, wired into [`.github/workflows/costs-validate.yml`](../../../.github/workflows/costs-validate.yml) and exposed as `pnpm costs:featured`.

Fails on:

1. A category with fewer than 2 featured models — pages would silently vanish.
2. A featured row whose `replaced_by_model_id` does not resolve in-file.
3. **A previously-published slug disappearing**, checked against a committed `costs/featured.lock.json` of generated slugs. This is the guard that matters: it makes a refresh run unable to delete indexed pages without the deletion showing up in review.

`refresh-costs.md` gains a **Featured models** subsection and one closing-checklist line: a new marquee model is a candidate for `"featured": true`; a deprecated featured model keeps its flag and must have a resolving `replaced_by_model_id`.

## Data flow

```
costs/{llm,stt,tts}.json  --(featured: true)-->  web/lib/featured.ts
                                                        |
                        +-------------------------------+-------------------+
                        |                                                   |
              generateStaticParams()                              web/app/sitemap.ts
                        |                                                   |
          /costs/compare/<a>-vs-<b>  (96 static pages)          /costs/sitemap.xml
                        |
              scripts/costs/check-featured.mjs  <--> costs/featured.lock.json
```

`web/lib/featured.ts` is the single boundary. It reads the dataset via the existing `web/lib/costs.ts` loader and exports the featured models, the slug map, and the ordered slug list. The route, the sitemap, and the CI check all consume that one module, so there is no second place where "which comparisons exist" is decided.

## Error handling

| Case                                   | Behavior                                                                  |
| -------------------------------------- | ------------------------------------------------------------------------- |
| Unknown pair slug                      | Static 404 via `dynamicParams = false`. No function invocation.           |
| `?m=` with unknown model ids           | Client-side picker ignores them, renders the empty state.                 |
| `?m=` with more than `MAX_COMPARE` ids | Picker truncates to the first 6, consistent with today's server behavior. |
| Featured model deprecated              | Page persists with a deprecation banner and successor link.               |
| Fewer than 2 featured in a category    | CI fails before deploy.                                                   |
| A published slug disappears            | CI fails against `featured.lock.json`.                                    |

## Testing

- `node --test scripts/costs/check-featured.test.mjs` — the three CI invariants, following the `check-stale.test.mjs` precedent.
- `pnpm site:build` — proves all 96 pages prerender and that `/compare` is no longer marked `ƒ (Dynamic)` in the route table.
- `check-jsonschema --schemafile costs/schema/<cat>.schema.json costs/<cat>.json` for the v2.3 flag. Note `pnpm costs:validate` is currently broken on macOS hosts by a pipx/Python 3.14 ABI issue; `/Users/r/.local/bin/check-jsonschema` works.
- Post-deploy: `curl -sI https://hail.so/costs/compare/<pair>` must show `x-vercel-cache: HIT`, and `curl -sI https://hail-costs.vercel.app/costs` must return 308.

## Consequences

- Function invocations on `/costs/compare`: 642K/12h → 0.
- 96 indexable, CDN-cached comparison pages replace an unbounded uncacheable space.
- One additive optional schema field (v2.3) and three runbook sections to update.
- Two edits required in hail-website that this repo cannot make; until they land, `robots.txt` still permits the query space, though the removal of crawlable `href`s already denies discovery of it.

## Alternatives rejected

**Machine-to-machine payments on the compare page.** The originating idea. Rejected: the traffic is a self-inflicted crawl loop, not demand. A 402 collects nothing from a scraper and costs Hail its AI-answer distribution.

**Keep `?m=` server-rendered, add caching and `Disallow`.** Least code churn, but ~9 × 10<sup>12</sup> URLs means a bot essentially never requests the same one twice, so cache hit rate against bots stays near zero.

**All within-category pairs** (2,926 + 666 + 465 = 4,057 pages). Maximum coverage, but most combinations have no search demand, and mass near-identical pages read as thin content.

**Hand-written slug list.** Total control, but it drifts from `costs/*.json` as models come and go — precisely the sync property this design is built to guarantee.
