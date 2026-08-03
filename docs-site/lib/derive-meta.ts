/**
 * Title/description derivation for docs that carry no frontmatter.
 *
 * The corpus in `docs/public/` is plain markdown that also renders on GitHub,
 * so we deliberately don't require a YAML header on every file — the rule for
 * this folder is "drop a .md in and it ships". Frontmatter still wins when a
 * file has it; these are only the fallbacks.
 */

/** First `# H1`, falling back to the filename-ish slug the caller supplies. */
export function deriveTitle(source: string, fallback = "Untitled"): string {
  const m = source.match(/^#\s+(.+?)\s*$/m);
  return m ? m[1].trim() : fallback;
}

/**
 * First real paragraph after the H1 — skips blank lines, blockquotes, badges,
 * and any HTML/comment block, so the sidebar and <meta> don't get a shields.io
 * image as the summary. Collapsed to one line and stripped of inline markdown.
 */
export function deriveDescription(source: string): string | undefined {
  const body = source.replace(/^#\s+.+$/m, "");
  for (const block of body.split(/\n\s*\n/)) {
    const t = block.trim();
    if (!t) continue;
    if (/^[>#\-*|`<]|^!\[|^\[!/.test(t)) continue;
    return t
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // links → text
      .replace(/[*_`]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }
  return undefined;
}
