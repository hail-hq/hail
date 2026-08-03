/**
 * Single source of truth for the docs/public path → URL slug rule.
 *
 * Two callers must agree exactly or the drift guard is meaningless:
 *   - lib/source.ts   (runtime: Fumadocs route + link generation)
 *   - scripts/urls.mjs (build: the urls.json snapshot hail-website checks against)
 *
 * Plain .mjs so the node script and the TS app can both import it without a
 * build step. Keep it dependency-free.
 */

/**
 * `"setup/README.md"` → `["setup"]`, `"README.md"` → `[]`, everything else
 * drops the `.md` and splits on `/`. Only an index file — README as the last
 * path segment — collapses to its folder, matching how GitHub renders a
 * directory's README. A directory that happens to be named README is left
 * intact.
 */
export function slugSegments(path) {
  const parts = path.replace(/\.mdx?$/, "").split("/");
  if (parts[parts.length - 1] === "README") parts.pop();
  return parts;
}
