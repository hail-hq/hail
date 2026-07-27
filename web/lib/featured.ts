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
