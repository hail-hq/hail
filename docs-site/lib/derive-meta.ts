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
 * A block that is NOT prose: heading, blockquote, table row, list item, fenced
 * code, thematic break, HTML/comment, image, or GitHub alert. Only block-LEVEL
 * markers count — an inline construct that merely *starts* a paragraph (inline
 * `code`, **bold**, _italic_, a [link](...)) is still prose, so those first
 * characters must not disqualify the block. Requiring a space after list
 * markers is what separates a `- item` list from a `-1` value or a `*note*`.
 */
function isNonProseBlock(t: string): boolean {
  return (
    /^(?:>|#|\||<|!\[|\[!|```|~~~)/.test(t) || // quote, heading, table, html, image, alert, fence
    /^[-*+]\s/.test(t) || // unordered list item (marker + space)
    /^\d+\.\s/.test(t) || // ordered list item
    /^([-*_])\1{2,}\s*$/.test(t) // thematic break: ---, ***, ___
  );
}

/**
 * First real paragraph after the H1 — skips blank lines and any non-prose block
 * (see isNonProseBlock), so the sidebar and <meta> don't get a shields.io image
 * or a table as the summary. Collapsed to one line and stripped of inline
 * markdown. A paragraph that opens with inline code or emphasis is still prose.
 */
export function deriveDescription(source: string): string | undefined {
  const body = source.replace(/^#\s+.+$/m, "");
  for (const block of body.split(/\n\s*\n/)) {
    const t = block.trim();
    if (!t || isNonProseBlock(t)) continue;
    return t
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // links → text
      .replace(/[*_`]/g, "")
      .replace(/\s+/g, " ")
      .trim();
  }
  return undefined;
}
