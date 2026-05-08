# Docs Site MVP — Fumadocs + Costs Pricing Tables

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a public docs site that renders interactive pricing tables for LLM, STT, and TTS providers from `costs/*.json`. Hosted on Vercel; bootstrap done in `docs/`. Multi-zones rewrite from `hail.so` and content migration of existing markdown are explicitly deferred to follow-ups.

**Architecture:** Next.js App Router rooted in `docs/`. Fumadocs UI provides theme + layout shell. Three pricing pages (`/docs/costs/{llm,stt,tts}`) are custom server components that read the JSON files from disk via `fs.readFile`, validate against TypeScript types, and pass typed data to client components built on TanStack Table headless primitives. Filter/sort state is mirrored to the URL via `nuqs` so every view is shareable. `basePath: '/docs'` so URLs work standalone (`vercel.app/docs/...`) and survive a future multi-zones proxy from `hail.so/docs/...`. Tailwind v4 for styling. No database, no API routes, no auth.

**Tech Stack:** Next.js 15 (App Router, React 19), Fumadocs 15, TanStack Table v8 (headless), `nuqs` for URL state, Tailwind v4, TypeScript 5.

---

## File Structure

**Created:**

- `docs/package.json` — Next.js + Fumadocs app, scoped as a pnpm workspace member
- `docs/tsconfig.json`
- `docs/next.config.ts` — `basePath: '/docs'`, image config, etc.
- `docs/source.config.ts` — Fumadocs MDX source pointing to `./content` (excludes `docs/superpowers/`, `docs/setup/`, etc.)
- `docs/postcss.config.mjs` — Tailwind v4
- `docs/app/layout.tsx` — root HTML layout
- `docs/app/global.css` — `@import "tailwindcss";`
- `docs/app/(docs)/layout.tsx` — Fumadocs `DocsLayout` wrapper
- `docs/app/(docs)/page.tsx` — `/docs/` landing page (placeholder)
- `docs/app/(docs)/costs/page.tsx` — `/docs/costs` index linking to the three subpages
- `docs/app/(docs)/costs/llm/page.tsx` — LLM pricing table
- `docs/app/(docs)/costs/stt/page.tsx` — STT pricing table
- `docs/app/(docs)/costs/tts/page.tsx` — TTS pricing table
- `docs/lib/costs.ts` — `loadLLM()`, `loadSTT()`, `loadTTS()` server-side loaders
- `docs/lib/types.ts` — TypeScript types matching the JSON Schemas
- `docs/components/pricing-table.tsx` — generic client component (TanStack + nuqs)
- `docs/components/columns.ts` — column factories per category
- `docs/components/stale-badge.tsx` — visual badge when `last_verified > 30d`
- `docs/components/agent-banner.tsx` — "Agents: fetch JSON directly →" affordance
- `docs/content/.gitkeep` — placeholder; future MDX content migration target
- `pnpm-workspace.yaml` (created if missing) or modified to include `docs`

**Modified:**

- `package.json` (root) — add `site:dev`, `site:build`, `site:start` scripts (filtered to `docs` workspace)
- `.gitignore` (root) — add `docs/.next/`, `docs/node_modules/`, `docs/out/` if not already covered

**Boundaries:** `docs/lib/` is server-only (`'server-only'` import). `docs/components/pricing-table.tsx` is the only client component. Pages are server components that load data and pass it down. No prop drilling beyond two levels.

---

## Task 1: Add `docs/` as a pnpm workspace and scaffold Next.js + Fumadocs + Tailwind

**Files:**

- Create: `pnpm-workspace.yaml` (if missing) or modify
- Create: `docs/package.json`, `docs/tsconfig.json`, `docs/next.config.ts`, `docs/postcss.config.mjs`, `docs/app/global.css`, `docs/app/layout.tsx`, `docs/.gitignore` (small)
- Modify: root `.gitignore`

- [ ] **Step 1: Add `docs` as a pnpm workspace member**

Read root `pnpm-workspace.yaml` (create if missing) and add `docs` to the `packages` list. If creating fresh, the file should be:

```yaml
packages:
  - "docs"
```

If a workspace file already exists, add `'docs'` to its `packages` array — preserve other entries.

- [ ] **Step 2: Create `docs/package.json`**

```json
{
  "name": "@hail-hq/docs-site",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "next dev --port 3001",
    "build": "next build",
    "start": "next start --port 3001",
    "lint": "next lint"
  },
  "dependencies": {
    "next": "^15.0.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0",
    "fumadocs-ui": "^15.0.0",
    "fumadocs-core": "^15.0.0",
    "fumadocs-mdx": "^11.0.0",
    "@tanstack/react-table": "^8.20.0",
    "nuqs": "^2.0.0"
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "@types/react": "^19.0.0",
    "@types/react-dom": "^19.0.0",
    "tailwindcss": "^4.0.0",
    "@tailwindcss/postcss": "^4.0.0",
    "typescript": "^5.5.0",
    "postcss": "^8.4.0"
  }
}
```

Use `latest` if any dependency above doesn't resolve at install time — the implementer should run `pnpm install` and adjust versions to whatever stable resolves cleanly.

- [ ] **Step 3: Create `docs/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["dom", "dom.iterable", "esnext"],
    "allowJs": false,
    "skipLibCheck": true,
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "module": "esnext",
    "moduleResolution": "bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "jsx": "preserve",
    "incremental": true,
    "plugins": [{ "name": "next" }],
    "paths": {
      "@/*": ["./*"]
    }
  },
  "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
  "exclude": ["node_modules"]
}
```

- [ ] **Step 4: Create `docs/next.config.ts`**

```ts
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  basePath: "/docs",
  reactStrictMode: true,
  experimental: {
    typedRoutes: true,
  },
};

export default nextConfig;
```

- [ ] **Step 5: Create `docs/postcss.config.mjs`**

```js
export default {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};
```

- [ ] **Step 6: Create `docs/app/global.css`**

```css
@import "tailwindcss";
@import "fumadocs-ui/css/neutral.css";
@import "fumadocs-ui/css/preset.css";
```

- [ ] **Step 7: Create `docs/app/layout.tsx`**

```tsx
import type { ReactNode } from "react";
import { RootProvider } from "fumadocs-ui/provider";
import "./global.css";

export const metadata = {
  title: "Hail Docs",
  description:
    "Documentation and pricing data for Hail — the universal communication platform for AI agents.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <RootProvider>{children}</RootProvider>
      </body>
    </html>
  );
}
```

- [ ] **Step 8: Create `docs/.gitignore`** (or update root `.gitignore`)

Add to root `.gitignore`:

```
docs/.next/
docs/node_modules/
docs/out/
docs/next-env.d.ts
```

- [ ] **Step 9: Install dependencies**

```bash
cd /Users/r/playground/hail
pnpm install
```

Expected: pnpm resolves and installs Next.js, Fumadocs, TanStack, nuqs into `docs/node_modules/`.

- [ ] **Step 10: Verify the dev server starts**

```bash
cd /Users/r/playground/hail/docs
pnpm dev
```

Expected: server starts on port 3001 without errors. Curl `http://localhost:3001/docs/` — for now this returns 404 since no pages exist yet; that's expected and confirms the basePath rewrite is working. Stop the server.

- [ ] **Step 11: Commit-ready report**

Status: DONE / DONE_WITH_CONCERNS / BLOCKED. Suggested commit:

```
chore(docs): scaffold Next.js + Fumadocs + Tailwind in docs/
```

---

## Task 2: Fumadocs source config + DocsLayout wrapper

**Files:**

- Create: `docs/source.config.ts`, `docs/lib/source.ts`, `docs/app/(docs)/layout.tsx`, `docs/app/(docs)/page.tsx`, `docs/content/.gitkeep`

- [ ] **Step 1: Create `docs/source.config.ts`**

```ts
import { defineConfig, defineDocs } from "fumadocs-mdx/config";

export const docs = defineDocs({
  dir: "content",
});

export default defineConfig();
```

- [ ] **Step 2: Create `docs/lib/source.ts`**

```ts
import { docs } from "@/.source";
import { loader } from "fumadocs-core/source";

export const source = loader({
  baseUrl: "/docs",
  source: docs.toFumadocsSource(),
});
```

- [ ] **Step 3: Create empty content directory**

```bash
mkdir -p docs/content
touch docs/content/.gitkeep
```

- [ ] **Step 4: Create `docs/app/(docs)/layout.tsx`**

```tsx
import type { ReactNode } from "react";
import { DocsLayout } from "fumadocs-ui/layouts/docs";
import { source } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.pageTree}
      nav={{ title: "Hail" }}
      sidebar={{
        defaultOpenLevel: 0,
      }}
    >
      {children}
    </DocsLayout>
  );
}
```

- [ ] **Step 5: Create `docs/app/(docs)/page.tsx`**

```tsx
import Link from "next/link";

export default function HomePage() {
  return (
    <main className="prose dark:prose-invert max-w-3xl mx-auto p-8">
      <h1>Hail Docs</h1>
      <p>
        Documentation and pricing data for Hail — the universal communication
        platform for AI agents.
      </p>
      <h2>Available now</h2>
      <ul>
        <li>
          <Link href="/docs/costs">Model costs</Link> — interactive pricing
          tables for LLM, STT, and TTS providers
        </li>
      </ul>
      <h2>Coming soon</h2>
      <ul>
        <li>
          Architecture overview, setup guides, ops runbooks (currently in{" "}
          <code>docs/</code> as plain markdown)
        </li>
        <li>
          OpenAPI reference (currently at <code>openapi/openapi.yaml</code>)
        </li>
        <li>MCP server reference</li>
      </ul>
    </main>
  );
}
```

- [ ] **Step 6: Run a quick build to ensure Fumadocs MDX postinstall ran and `.source` resolves**

```bash
cd /Users/r/playground/hail/docs
pnpm fumadocs-mdx
```

Expected: emits `.source/index.ts` (or similar). If `fumadocs-mdx` CLI doesn't exist as a binary, this step is automatic via the next build pipeline — try `pnpm dev` instead and confirm no errors.

- [ ] **Step 7: Verify `/docs/` renders in browser**

```bash
pnpm dev
```

Open `http://localhost:3001/docs/` — should show "Hail Docs" page with Fumadocs sidebar/header chrome (sidebar will be empty since `content/` is empty). Stop server.

- [ ] **Step 8: Commit-ready report**

Status. Suggested commit:

```
feat(docs): add Fumadocs DocsLayout and root /docs landing page
```

---

## Task 3: Cost data loaders + TypeScript types

**Files:**

- Create: `docs/lib/types.ts`, `docs/lib/costs.ts`

- [ ] **Step 1: Create `docs/lib/types.ts`** with types matching the JSON Schemas

```ts
export type Modality = "text" | "image" | "audio" | "video";

export type CommonFields = {
  provider: string;
  provider_url?: string;
  model_id: string;
  display_name: string;
  last_verified: string;
  verified_by: string;
  source_url: string;
  notes?: string;
};

export type LLMRow = CommonFields & {
  model_family?: string;
  release_date?: string;
  knowledge_cutoff?: string;
  context_window: number;
  max_output_tokens: number;
  input_per_mtok_usd: number;
  output_per_mtok_usd: number;
  cached_input_per_mtok_usd?: number;
  modalities: { input: Modality[]; output: Modality[] };
  tool_use?: boolean;
  structured_output?: boolean;
};

export type STTRow = CommonFields & {
  price_per_minute_usd: number;
  price_per_minute_batch_usd?: number;
  languages: string[] | string;
  streaming: boolean;
  realtime?: boolean;
  diarization?: "included" | "extra-cost" | "unsupported";
  wer_benchmark?: { dataset: string; wer_pct: number; source_url?: string };
  time_to_first_word_ms?: number;
};

export type TTSRow = CommonFields & {
  price_per_1m_chars_usd: number;
  voice_quality: "standard" | "neural" | "cloned";
  voice_count?: number;
  languages: string[] | string;
  ssml_support?: boolean;
  voice_cloning?:
    | boolean
    | { price_usd: number; unit: "per-clone" | "monthly" | "per-1m-chars" };
  output_formats?: string[];
  time_to_first_byte_ms?: number;
};

export type CostsFile<T> = {
  version: 1;
  updated: string;
  license: "CC-BY-4.0";
  models: T[];
};
```

- [ ] **Step 2: Create `docs/lib/costs.ts`**

```ts
import "server-only";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { CostsFile, LLMRow, STTRow, TTSRow } from "./types";

const REPO_ROOT = join(process.cwd(), "..");

async function loadFile<T>(name: "llm" | "stt" | "tts"): Promise<CostsFile<T>> {
  const path = join(REPO_ROOT, "costs", `${name}.json`);
  const raw = await readFile(path, "utf-8");
  return JSON.parse(raw) as CostsFile<T>;
}

export async function loadLLM(): Promise<CostsFile<LLMRow>> {
  return loadFile<LLMRow>("llm");
}
export async function loadSTT(): Promise<CostsFile<STTRow>> {
  return loadFile<STTRow>("stt");
}
export async function loadTTS(): Promise<CostsFile<TTSRow>> {
  return loadFile<TTSRow>("tts");
}

const MS_PER_DAY = 1000 * 60 * 60 * 24;
export function isStale(
  lastVerified: string,
  maxAgeDays = 30,
  now = new Date(),
): boolean {
  const verifiedMs = new Date(lastVerified + "T00:00:00Z").getTime();
  return now.getTime() - verifiedMs > maxAgeDays * MS_PER_DAY;
}

export function daysSince(lastVerified: string, now = new Date()): number {
  const verifiedMs = new Date(lastVerified + "T00:00:00Z").getTime();
  return Math.floor((now.getTime() - verifiedMs) / MS_PER_DAY);
}
```

The `process.cwd()` resolves to `docs/` when Next.js runs from `docs/`. `..` walks up to repo root. If running with the dev server in monorepo root, this might break — implementer should verify this works by hitting `/docs/costs/llm` later and seeing data render. Fallback: hardcode a path resolution via `import.meta.url`.

- [ ] **Step 3: Quick smoke test**

Add a temporary debug route at `docs/app/(docs)/debug/page.tsx`:

```tsx
import { loadLLM } from "@/lib/costs";

export default async function DebugPage() {
  const data = await loadLLM();
  return (
    <main className="p-8">
      <h1>LLM data smoke test</h1>
      <p>
        Loaded {data.models.length} models, version {data.version}, updated{" "}
        {data.updated}.
      </p>
      <pre>{JSON.stringify(data.models[0], null, 2)}</pre>
    </main>
  );
}
```

Run `pnpm dev` and hit `http://localhost:3001/docs/debug`. Should show "Loaded 5 models..." and the first model's JSON.

If it works, **delete** `docs/app/(docs)/debug/page.tsx` before commit.

- [ ] **Step 4: Commit-ready report**

Suggested commit:

```
feat(docs): add cost data loaders and TypeScript types
```

---

## Task 4: Generic PricingTable component (TanStack + nuqs)

**Files:**

- Create: `docs/components/pricing-table.tsx`

The component is generic over row type. It accepts data + columns (TanStack ColumnDef[]) + filter config + a "killer metric" key to pin/highlight. URL state via nuqs.

- [ ] **Step 1: Create `docs/components/pricing-table.tsx`**

```tsx
"use client";

import { useState } from "react";
import {
  ColumnDef,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  SortingState,
  useReactTable,
  type ColumnFiltersState,
} from "@tanstack/react-table";
import { useQueryState, parseAsString, parseAsArrayOf } from "nuqs";

export interface PricingTableProps<T> {
  data: T[];
  columns: ColumnDef<T>[];
  searchPlaceholder?: string;
  searchableField: keyof T & string;
}

export function PricingTable<T>({
  data,
  columns,
  searchPlaceholder = "Search providers or models…",
  searchableField,
}: PricingTableProps<T>) {
  const [search, setSearch] = useQueryState("q", parseAsString.withDefault(""));
  const [providers, setProviders] = useQueryState(
    "p",
    parseAsArrayOf(parseAsString).withDefault([]),
  );
  const [sorting, setSorting] = useState<SortingState>([]);

  const columnFilters: ColumnFiltersState = [];
  if (providers.length > 0) {
    columnFilters.push({ id: "provider", value: providers });
  }

  const filteredData = search
    ? data.filter((row) => {
        const v = (row as any)[searchableField];
        return String(v ?? "")
          .toLowerCase()
          .includes(search.toLowerCase());
      })
    : data;

  const table = useReactTable({
    data: filteredData,
    columns,
    state: { sorting, columnFilters },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getSortedRowModel: getSortedRowModel(),
    filterFns: {
      arrayIncludes: (row, id, value) =>
        (value as string[]).includes(row.getValue(id) as string),
    },
  });

  const allProviders = Array.from(
    new Set(data.map((r) => (r as any).provider as string)),
  ).sort();

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row gap-3">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value || null)}
          placeholder={searchPlaceholder}
          className="flex-1 px-3 py-2 rounded-md border border-fd-border bg-fd-background"
        />
        <div className="flex flex-wrap gap-2">
          {allProviders.map((p) => {
            const active = providers.includes(p);
            return (
              <button
                key={p}
                type="button"
                onClick={() => {
                  setProviders(
                    active
                      ? providers.filter((x) => x !== p)
                      : [...providers, p],
                  );
                }}
                className={`px-3 py-1 rounded-full text-sm border ${
                  active
                    ? "bg-fd-primary text-fd-primary-foreground border-fd-primary"
                    : "border-fd-border text-fd-muted-foreground"
                }`}
              >
                {p}
              </button>
            );
          })}
        </div>
      </div>

      <div className="hidden md:block overflow-x-auto rounded-lg border border-fd-border">
        <table className="w-full text-sm">
          <thead className="bg-fd-muted">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    onClick={h.column.getToggleSortingHandler()}
                    className="text-left px-3 py-2 font-medium cursor-pointer select-none"
                  >
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {{ asc: " ↑", desc: " ↓" }[
                      h.column.getIsSorted() as string
                    ] ?? ""}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="border-t border-fd-border hover:bg-fd-accent"
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-2">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="md:hidden space-y-3">
        {table.getRowModel().rows.map((row) => (
          <div key={row.id} className="rounded-lg border border-fd-border p-4">
            {row.getVisibleCells().map((cell) => (
              <div
                key={cell.id}
                className="flex justify-between gap-3 py-1 text-sm"
              >
                <div className="text-fd-muted-foreground">
                  {flexRender(
                    cell.column.columnDef.header,
                    cell.getContext() as any,
                  )}
                </div>
                <div className="font-medium text-right">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </div>
              </div>
            ))}
          </div>
        ))}
      </div>

      <p className="text-sm text-fd-muted-foreground">
        Showing {table.getRowModel().rows.length} of {data.length} models
        {search && ` matching "${search}"`}
        {providers.length > 0 && ` from ${providers.join(", ")}`}.
      </p>
    </div>
  );
}
```

This single component handles desktop table + mobile cards via responsive classes (`hidden md:block` / `md:hidden`).

- [ ] **Step 2: Add nuqs adapter to root provider**

Update `docs/app/layout.tsx` — wrap children with the nuqs `NuqsAdapter`:

```tsx
import type { ReactNode } from "react";
import { RootProvider } from "fumadocs-ui/provider";
import { NuqsAdapter } from "nuqs/adapters/next/app";
import "./global.css";

export const metadata = {
  title: "Hail Docs",
  description:
    "Documentation and pricing data for Hail — the universal communication platform for AI agents.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <NuqsAdapter>
          <RootProvider>{children}</RootProvider>
        </NuqsAdapter>
      </body>
    </html>
  );
}
```

- [ ] **Step 3: Commit-ready report**

```
feat(docs): add generic PricingTable component (TanStack + nuqs URL state)
```

---

## Task 5: Stale-row badge + Agents banner components

**Files:**

- Create: `docs/components/stale-badge.tsx`, `docs/components/agent-banner.tsx`

- [ ] **Step 1: Create `docs/components/stale-badge.tsx`**

```tsx
import { isStale, daysSince } from "@/lib/costs";

export function StaleBadge({ lastVerified }: { lastVerified: string }) {
  if (!isStale(lastVerified)) return null;
  const days = daysSince(lastVerified);
  return (
    <span
      className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs bg-amber-100 text-amber-900 dark:bg-amber-900/30 dark:text-amber-200"
      title={`Last verified ${lastVerified} (${days} days ago). Help us refresh — see /docs/contributing.`}
    >
      ⚠ stale ({days}d)
    </span>
  );
}
```

- [ ] **Step 2: Create `docs/components/agent-banner.tsx`**

```tsx
export function AgentBanner({ jsonUrl }: { jsonUrl: string }) {
  return (
    <div className="rounded-lg border border-fd-border bg-fd-muted/50 p-4 my-4 text-sm">
      <strong>Building an agent?</strong> Fetch the structured JSON directly:{" "}
      <a href={jsonUrl} className="font-mono underline break-all">
        {jsonUrl}
      </a>
      . Schema-validated, CC-BY-4.0, refreshed weekly. See{" "}
      <a
        href="https://github.com/hail-hq/hail/tree/main/costs"
        className="underline"
      >
        the dataset on GitHub
      </a>{" "}
      for the full contract.
    </div>
  );
}
```

- [ ] **Step 3: Commit-ready report**

```
feat(docs): add stale-row badge and agents banner components
```

---

## Task 6: LLM pricing page

**Files:**

- Create: `docs/components/columns.ts` (LLM columns first; STT/TTS added in their tasks), `docs/app/(docs)/costs/llm/page.tsx`

- [ ] **Step 1: Create `docs/components/columns.ts` with LLM column factory**

```ts
import { ColumnDef } from '@tanstack/react-table';
import type { LLMRow } from '@/lib/types';
import { StaleBadge } from './stale-badge';

export const llmColumns: ColumnDef<LLMRow>[] = [
  {
    accessorKey: 'provider',
    header: 'Provider',
    cell: ({ row }) => <span className="font-medium">{row.original.provider}</span>,
    filterFn: 'arrayIncludes' as any,
  },
  {
    accessorKey: 'display_name',
    header: 'Model',
    cell: ({ row }) => (
      <div>
        <div className="font-medium">{row.original.display_name}</div>
        <div className="text-xs text-fd-muted-foreground font-mono">{row.original.model_id}</div>
      </div>
    ),
  },
  {
    accessorKey: 'output_per_mtok_usd',
    header: 'Output $/MTok',
    cell: ({ row }) => (
      <span className="font-mono">${row.original.output_per_mtok_usd.toFixed(2)}</span>
    ),
    sortingFn: 'basic',
  },
  {
    accessorKey: 'input_per_mtok_usd',
    header: 'Input $/MTok',
    cell: ({ row }) => (
      <span className="font-mono">${row.original.input_per_mtok_usd.toFixed(2)}</span>
    ),
    sortingFn: 'basic',
  },
  {
    accessorKey: 'cached_input_per_mtok_usd',
    header: 'Cached $/MTok',
    cell: ({ row }) =>
      row.original.cached_input_per_mtok_usd !== undefined ? (
        <span className="font-mono">${row.original.cached_input_per_mtok_usd.toFixed(4)}</span>
      ) : (
        <span className="text-fd-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: 'context_window',
    header: 'Context',
    cell: ({ row }) => <span className="font-mono">{row.original.context_window.toLocaleString()}</span>,
  },
  {
    accessorKey: 'last_verified',
    header: 'Verified',
    cell: ({ row }) => (
      <div className="flex items-center gap-2 text-xs">
        <span className="font-mono text-fd-muted-foreground">{row.original.last_verified}</span>
        <StaleBadge lastVerified={row.original.last_verified} />
      </div>
    ),
  },
];
```

- [ ] **Step 2: Create `docs/app/(docs)/costs/llm/page.tsx`**

```tsx
import { loadLLM } from "@/lib/costs";
import { PricingTable } from "@/components/pricing-table";
import { llmColumns } from "@/components/columns";
import { AgentBanner } from "@/components/agent-banner";

export const metadata = {
  title: "LLM Pricing — Hail",
  description:
    "Compare prices and capabilities of large language model providers. Updated weekly.",
};

export default async function LLMPricingPage() {
  const data = await loadLLM();
  return (
    <main className="max-w-6xl mx-auto p-6 space-y-6">
      <header>
        <h1 className="text-3xl font-bold">LLM Pricing</h1>
        <p className="text-fd-muted-foreground mt-2">
          Cost and capability data for {data.models.length} large language
          models. Updated {data.updated}. Click any column header to sort; the
          killer metric (output $/MTok) is the first sortable column.
        </p>
      </header>
      <AgentBanner jsonUrl="https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json" />
      <PricingTable
        data={data.models}
        columns={llmColumns}
        searchableField="display_name"
        searchPlaceholder="Search models or providers…"
      />
    </main>
  );
}
```

- [ ] **Step 3: Verify in browser**

```bash
pnpm dev
```

Open `http://localhost:3001/docs/costs/llm`. Expected: 5 LLM rows render, sort works on price columns, provider filter buttons toggle, search filters by model name. URL updates as you filter (e.g., `?p=Anthropic`). Stale-row badges appear for any row > 30 days old (none today).

- [ ] **Step 4: Commit-ready report**

```
feat(docs): add LLM pricing page
```

---

## Task 7: STT pricing page

**Files:**

- Modify: `docs/components/columns.ts` (add `sttColumns`)
- Create: `docs/app/(docs)/costs/stt/page.tsx`

- [ ] **Step 1: Append `sttColumns` to `docs/components/columns.ts`**

Use Edit, add after the `llmColumns` definition:

```ts
import type { STTRow } from '@/lib/types';

export const sttColumns: ColumnDef<STTRow>[] = [
  {
    accessorKey: 'provider',
    header: 'Provider',
    cell: ({ row }) => <span className="font-medium">{row.original.provider}</span>,
    filterFn: 'arrayIncludes' as any,
  },
  {
    accessorKey: 'display_name',
    header: 'Model',
    cell: ({ row }) => (
      <div>
        <div className="font-medium">{row.original.display_name}</div>
        <div className="text-xs text-fd-muted-foreground font-mono">{row.original.model_id}</div>
      </div>
    ),
  },
  {
    accessorKey: 'price_per_minute_usd',
    header: '$/min',
    cell: ({ row }) => (
      <span className="font-mono">${row.original.price_per_minute_usd.toFixed(4)}</span>
    ),
    sortingFn: 'basic',
  },
  {
    accessorKey: 'price_per_minute_batch_usd',
    header: '$/min (batch)',
    cell: ({ row }) =>
      row.original.price_per_minute_batch_usd !== undefined ? (
        <span className="font-mono">${row.original.price_per_minute_batch_usd.toFixed(4)}</span>
      ) : (
        <span className="text-fd-muted-foreground">—</span>
      ),
  },
  {
    accessorKey: 'streaming',
    header: 'Streaming',
    cell: ({ row }) => (row.original.streaming ? '✓' : '—'),
  },
  {
    accessorKey: 'languages',
    header: 'Languages',
    cell: ({ row }) => {
      const l = row.original.languages;
      return Array.isArray(l) ? l.join(', ') : l;
    },
  },
  {
    accessorKey: 'last_verified',
    header: 'Verified',
    cell: ({ row }) => (
      <div className="flex items-center gap-2 text-xs">
        <span className="font-mono text-fd-muted-foreground">{row.original.last_verified}</span>
        <StaleBadge lastVerified={row.original.last_verified} />
      </div>
    ),
  },
];
```

- [ ] **Step 2: Create `docs/app/(docs)/costs/stt/page.tsx`**

```tsx
import { loadSTT } from "@/lib/costs";
import { PricingTable } from "@/components/pricing-table";
import { sttColumns } from "@/components/columns";
import { AgentBanner } from "@/components/agent-banner";

export const metadata = {
  title: "STT Pricing — Hail",
  description:
    "Compare prices and capabilities of speech-to-text providers. Updated weekly.",
};

export default async function STTPricingPage() {
  const data = await loadSTT();
  return (
    <main className="max-w-6xl mx-auto p-6 space-y-6">
      <header>
        <h1 className="text-3xl font-bold">STT Pricing</h1>
        <p className="text-fd-muted-foreground mt-2">
          Cost and capability data for {data.models.length} speech-to-text
          models. Updated {data.updated}.
        </p>
      </header>
      <AgentBanner jsonUrl="https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json" />
      <PricingTable
        data={data.models}
        columns={sttColumns}
        searchableField="display_name"
        searchPlaceholder="Search models or providers…"
      />
    </main>
  );
}
```

- [ ] **Step 3: Verify**

`http://localhost:3001/docs/costs/stt` — 8 rows render, sort/filter work.

- [ ] **Step 4: Commit-ready report**

```
feat(docs): add STT pricing page
```

---

## Task 8: TTS pricing page

**Files:**

- Modify: `docs/components/columns.ts` (add `ttsColumns`)
- Create: `docs/app/(docs)/costs/tts/page.tsx`

- [ ] **Step 1: Append `ttsColumns` to `docs/components/columns.ts`**

```ts
import type { TTSRow } from '@/lib/types';

export const ttsColumns: ColumnDef<TTSRow>[] = [
  {
    accessorKey: 'provider',
    header: 'Provider',
    cell: ({ row }) => <span className="font-medium">{row.original.provider}</span>,
    filterFn: 'arrayIncludes' as any,
  },
  {
    accessorKey: 'display_name',
    header: 'Model',
    cell: ({ row }) => (
      <div>
        <div className="font-medium">{row.original.display_name}</div>
        <div className="text-xs text-fd-muted-foreground font-mono">{row.original.model_id}</div>
      </div>
    ),
  },
  {
    accessorKey: 'price_per_1m_chars_usd',
    header: '$/1M chars',
    cell: ({ row }) => (
      <span className="font-mono">${row.original.price_per_1m_chars_usd.toFixed(2)}</span>
    ),
    sortingFn: 'basic',
  },
  {
    accessorKey: 'voice_quality',
    header: 'Quality',
  },
  {
    accessorKey: 'voice_cloning',
    header: 'Cloning',
    cell: ({ row }) => {
      const vc = row.original.voice_cloning;
      if (vc === undefined) return '—';
      if (typeof vc === 'boolean') return vc ? '✓' : '—';
      return `$${vc.price_usd} ${vc.unit}`;
    },
  },
  {
    accessorKey: 'languages',
    header: 'Languages',
    cell: ({ row }) => {
      const l = row.original.languages;
      return Array.isArray(l) ? l.join(', ') : l;
    },
  },
  {
    accessorKey: 'last_verified',
    header: 'Verified',
    cell: ({ row }) => (
      <div className="flex items-center gap-2 text-xs">
        <span className="font-mono text-fd-muted-foreground">{row.original.last_verified}</span>
        <StaleBadge lastVerified={row.original.last_verified} />
      </div>
    ),
  },
];
```

- [ ] **Step 2: Create `docs/app/(docs)/costs/tts/page.tsx`**

```tsx
import { loadTTS } from "@/lib/costs";
import { PricingTable } from "@/components/pricing-table";
import { ttsColumns } from "@/components/columns";
import { AgentBanner } from "@/components/agent-banner";

export const metadata = {
  title: "TTS Pricing — Hail",
  description:
    "Compare prices and capabilities of text-to-speech providers. Updated weekly.",
};

export default async function TTSPricingPage() {
  const data = await loadTTS();
  return (
    <main className="max-w-6xl mx-auto p-6 space-y-6">
      <header>
        <h1 className="text-3xl font-bold">TTS Pricing</h1>
        <p className="text-fd-muted-foreground mt-2">
          Cost and capability data for {data.models.length} text-to-speech
          models. Updated {data.updated}.
        </p>
      </header>
      <AgentBanner jsonUrl="https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json" />
      <PricingTable
        data={data.models}
        columns={ttsColumns}
        searchableField="display_name"
        searchPlaceholder="Search models or providers…"
      />
    </main>
  );
}
```

- [ ] **Step 3: Verify**

`http://localhost:3001/docs/costs/tts` — 6 rows render.

- [ ] **Step 4: Commit-ready report**

```
feat(docs): add TTS pricing page
```

---

## Task 9: Costs index page

**Files:**

- Create: `docs/app/(docs)/costs/page.tsx`

- [ ] **Step 1: Create the index page**

```tsx
import Link from "next/link";
import { loadLLM, loadSTT, loadTTS } from "@/lib/costs";

export const metadata = {
  title: "Model Costs — Hail",
  description:
    "Public, validated pricing data for LLM, STT, and TTS providers.",
};

export default async function CostsIndexPage() {
  const [llm, stt, tts] = await Promise.all([loadLLM(), loadSTT(), loadTTS()]);
  return (
    <main className="max-w-4xl mx-auto p-6 space-y-6">
      <header>
        <h1 className="text-3xl font-bold">Model costs</h1>
        <p className="text-fd-muted-foreground mt-2">
          Public, validated pricing and capability data for AI model providers.
          Schema-validated, CC-BY-4.0, refreshed weekly.
        </p>
      </header>

      <div className="grid sm:grid-cols-3 gap-4">
        <Link
          href="/docs/costs/llm"
          className="block rounded-lg border border-fd-border p-6 hover:bg-fd-accent transition"
        >
          <h2 className="text-xl font-semibold">LLMs</h2>
          <p className="text-sm text-fd-muted-foreground mt-1">
            {llm.models.length} models
          </p>
        </Link>
        <Link
          href="/docs/costs/stt"
          className="block rounded-lg border border-fd-border p-6 hover:bg-fd-accent transition"
        >
          <h2 className="text-xl font-semibold">Speech-to-Text</h2>
          <p className="text-sm text-fd-muted-foreground mt-1">
            {stt.models.length} models
          </p>
        </Link>
        <Link
          href="/docs/costs/tts"
          className="block rounded-lg border border-fd-border p-6 hover:bg-fd-accent transition"
        >
          <h2 className="text-xl font-semibold">Text-to-Speech</h2>
          <p className="text-sm text-fd-muted-foreground mt-1">
            {tts.models.length} models
          </p>
        </Link>
      </div>

      <section className="space-y-2">
        <h2 className="text-xl font-semibold">For agents and scripts</h2>
        <p className="text-fd-muted-foreground">
          The pricing data is also published as raw JSON, suitable for
          programmatic consumption:
        </p>
        <ul className="font-mono text-sm space-y-1">
          <li>
            <a
              className="underline"
              href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json"
            >
              raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json
            </a>
          </li>
          <li>
            <a
              className="underline"
              href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json"
            >
              raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json
            </a>
          </li>
          <li>
            <a
              className="underline"
              href="https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json"
            >
              raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json
            </a>
          </li>
        </ul>
      </section>
    </main>
  );
}
```

- [ ] **Step 2: Verify**

`http://localhost:3001/docs/costs` shows three cards with model counts and the agents-section.

- [ ] **Step 3: Commit-ready report**

```
feat(docs): add costs index page
```

---

## Task 10: Root pnpm scripts + Vercel config

**Files:**

- Modify: root `package.json`
- Create: `vercel.json` (root)

- [ ] **Step 1: Add site scripts to root `package.json`**

Use Edit; add these to the `scripts` block (preserve existing `prepare`, `costs:validate`, `costs:stale`):

```json
"site:dev": "pnpm --filter @hail-hq/docs-site dev",
"site:build": "pnpm --filter @hail-hq/docs-site build",
"site:start": "pnpm --filter @hail-hq/docs-site start"
```

- [ ] **Step 2: Verify they run**

```bash
pnpm site:build
```

Expected: Next.js build succeeds, emits to `docs/.next/`.

- [ ] **Step 3: Create `vercel.json`** at the repo root for the docs project's Ignored Build Step

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "buildCommand": "pnpm site:build",
  "devCommand": "pnpm site:dev",
  "installCommand": "pnpm install --frozen-lockfile",
  "outputDirectory": "docs/.next",
  "ignoreCommand": "git diff --quiet HEAD^ HEAD -- docs/ costs/ pnpm-workspace.yaml package.json pnpm-lock.yaml"
}
```

The `ignoreCommand` returns 0 (skip build) when no relevant files changed in the last commit, non-zero when something docs-relevant changed (triggering a build).

- [ ] **Step 4: Commit-ready report**

```
chore(docs): add pnpm site scripts and Vercel config
```

---

## Task 11: Smoke test the full local site, then deploy

**No new files.** This task is verification + deployment kickoff.

- [ ] **Step 1: Build clean and serve production locally**

```bash
cd /Users/r/playground/hail
pnpm site:build
pnpm site:start
```

Open `http://localhost:3001/docs/`, click through to `/docs/costs`, then each of `/docs/costs/{llm,stt,tts}`. Verify:

- All 5 LLM rows / 8 STT rows / 6 TTS rows display
- Sort works on every numeric column (asc/desc toggle)
- Provider filter buttons add/remove rows
- Search input filters by model name
- URL state updates (`?q=…`, `?p=Anthropic`)
- Mobile cards layout works (resize browser narrow OR use devtools mobile emulator)
- "Agents: fetch JSON directly" banner is visible on each pricing page with the correct raw GitHub URL
- No console errors

If anything doesn't work, fix and re-verify before deploying.

- [ ] **Step 2: Deploy to Vercel**

The user will run this manually:

```bash
# From repo root, assuming Vercel CLI is installed and authenticated
vercel --prod
```

(Or set up the Vercel project in their dashboard pointing at this repo, with Build Command `pnpm site:build`, Output Directory `docs/.next`, Install Command `pnpm install`.)

Post-deploy: hit the deployment URL `/docs/costs/llm` and confirm the page renders with real data. If `loadLLM()` errors with a path issue (`process.cwd()` resolving differently on Vercel than locally), the implementer should swap to a path-resolution strategy that works in both environments — likely embedding the JSON files into the build via `import` statements:

```ts
import llmData from "../../costs/llm.json";
```

This is the fallback if filesystem reads don't work on Vercel. It also has the benefit of compile-time validation — TypeScript will catch shape mismatches.

- [ ] **Step 3: Commit-ready report**

```
chore(docs): smoke-test and ship MVP costs site
```

---

## Wrap-up

After all 11 tasks land, the Hail docs site is live at the Vercel deployment URL with three working pricing pages, schema-typed data flowing from `costs/*.json` through TypeScript loaders into TanStack Table-backed UI, with URL state, mobile cards, stale-row badges, and an agents-banner pointing at the raw JSON. Multi-zones rewrite from `hail.so/docs/*` is the only step remaining to put it on the production domain — that's a 5-line change in the landing repo.

**Deferred to follow-up plans (in priority order):**

1. **Multi-zones rewrite in landing repo** (1 file, ~5 lines): proxies `hail.so/docs/*` to the docs Vercel deployment. No URL churn since `basePath: '/docs'` is already in place.
2. **Compare view** (`/docs/costs/compare?models=a,b,c`): URL-state work is already there from `nuqs`; this is mostly a different page that filters down to 2–4 models and renders side-by-side.
3. **Content migration**: move existing `docs/architecture.md`, `docs/setup/`, `docs/contributing.md`, `docs/ops/` into `docs/content/` and let Fumadocs render them. Many cross-references to fix.
4. **OpenAPI rendering**: add `fumadocs-openapi`, point at `openapi/openapi.yaml`, render at `/docs/api`.
5. **Agent surfaces**: raw `.md` route handler, `llms.txt` + `llms-full.txt` generators, `sitemap.xml`. Each is a small additive route.
6. **MCP server extension** (separate plan): expose docs + costs via the existing MCP server at `mcp/`.
7. **Deferred items from `costs/` review** (see memory `project_costs_deferred_items.md`): I3 (root license note), I4 (`updated` field design), N1 (Cartesia SSML), N3 (script symlink), N4 (schema `$id` URLs — relevant once the site can host them).
