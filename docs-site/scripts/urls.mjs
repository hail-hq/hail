#!/usr/bin/env node
/**
 * Canonical list of published docs URLs, derived from the `docs/public/` tree
 * using the same rule as `docs-site/lib/source.ts` (README = folder index).
 *
 * Two consumers depend on this list staying honest:
 *   - hail-website's `lib/docs.ts` / sitemap, which lives in another repo and
 *     cannot import from here.
 *   - `urls.json`, the checked-in snapshot. `--check` diffs against it and
 *     exits non-zero on drift, so adding or renaming a file in docs/public/
 *     fails CI until the snapshot (and the website) are updated together.
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { slugSegments } from "../lib/doc-slug.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const CONTENT = resolve(here, "../../docs/public");
const SNAPSHOT = resolve(here, "../urls.json");

function walk(dir) {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) return walk(full);
    return name.endsWith(".md") ? [full] : [];
  });
}

// Same slug rule as lib/source.ts, via the shared helper — a bare "/docs" for
// the root index, "/docs/<segments>" otherwise.
const urls = walk(CONTENT)
  .map((file) => {
    const segments = slugSegments(relative(CONTENT, file).split("\\").join("/"));
    return segments.length ? "/docs/" + segments.join("/") : "/docs";
  })
  .sort();

if (process.argv.includes("--check")) {
  const expected = JSON.parse(readFileSync(SNAPSHOT, "utf8"));
  const same = JSON.stringify(expected) === JSON.stringify(urls);
  if (!same) {
    console.error("docs URL drift — docs/public/ no longer matches urls.json\n");
    console.error("  removed:", expected.filter((u) => !urls.includes(u)).join(", ") || "(none)");
    console.error("  added:  ", urls.filter((u) => !expected.includes(u)).join(", ") || "(none)");
    console.error("\nRegenerate with `pnpm docs:urls`, then update DOCS in hail-website/lib/docs.ts.");
    process.exit(1);
  }
  console.log(`docs URLs match urls.json (${urls.length} pages)`);
} else {
  writeFileSync(SNAPSHOT, JSON.stringify(urls, null, 2) + "\n");
  console.log(`wrote ${urls.length} URLs to urls.json`);
  for (const u of urls) console.log("  " + u);
}
