import 'server-only';
import { readFile } from 'node:fs/promises';
import { source } from '@/lib/source';
import { absoluteUrl } from '@/lib/url';

export const dynamic = 'force-static';

export async function GET() {
  const pages = source
    .getPages()
    .filter((p) => p.absolutePath)
    .sort((a, b) => a.absolutePath!.localeCompare(b.absolutePath!));

  const sections = await Promise.all(
    pages.map(async (p) => {
      const content = await readFile(p.absolutePath!, 'utf-8');
      return `<!-- source: ${absoluteUrl(p.url)} -->\n\n${content}`;
    }),
  );

  const body = `# Hail — full docs corpus

> Universal communication platform for AI agents. This file concatenates every prose docs page (MDX) into one plain-text document for LLM ingestion. Frontmatter is preserved per section. Pricing data lives at /llms.txt and /costs.md.

${sections.join('\n\n---\n\n')}
`;

  return new Response(body, {
    status: 200,
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'public, max-age=0, s-maxage=600, stale-while-revalidate=86400',
    },
  });
}
