import { llm, stt, tts } from '@/lib/costs';
import { renderCostsMarkdown } from '@/lib/markdown';

export const dynamic = 'force-static';

export function GET() {
  const md = renderCostsMarkdown(llm, stt, tts, new Date().toISOString());
  return new Response(md, {
    status: 200,
    headers: {
      'content-type': 'text/markdown; charset=utf-8',
      'cache-control': 'public, max-age=0, s-maxage=300, stale-while-revalidate=86400',
    },
  });
}
