import { readFile } from "node:fs/promises";
import path from "node:path";
import type { source } from "@/lib/source";

type DocsPage = NonNullable<ReturnType<typeof source.getPage>>;

// fumadocs-mdx's own page.data.getText("raw") resolves the file through a
// logical (URL-style) path joiner that treats a leading ".." as "nothing to
// pop" instead of a real filesystem traversal — it 404s for a content dir
// declared as "../docs/public" (source.config.ts), which is exactly ours:
// docs/public/ lives one level above docs-site/. Read the file directly
// instead, using `page.path` (content-dir-relative, e.g. "architecture.md"),
// which isn't run through that joiner.
const DOCS_PUBLIC_DIR = path.resolve(process.cwd(), "../docs/public");

/**
 * Every file in docs/public/ opens with its own `# H1` (see the comment in
 * app/(docs)/[[...slug]]/page.tsx) and has no custom MDX components, so the
 * raw file *is* the page's markdown — no re-rendering needed.
 */
export async function getDocsLLMText(page: DocsPage): Promise<string> {
  return readFile(path.join(DOCS_PUBLIC_DIR, page.path), "utf-8");
}
