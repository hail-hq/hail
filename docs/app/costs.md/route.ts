import { loadLLM, loadSTT, loadTTS } from '@/lib/costs';
import { renderCostsMarkdown } from '@/lib/markdown';

export const dynamic = 'force-static';

export async function GET() {
  const [llm, stt, tts] = await Promise.all([loadLLM(), loadSTT(), loadTTS()]);
  const md = renderCostsMarkdown(llm, stt, tts, new Date().toISOString());
  return new Response(md, {
    status: 200,
    headers: {
      'content-type': 'text/markdown; charset=utf-8',
      'cache-control': 'public, max-age=0, s-maxage=300, stale-while-revalidate=86400',
    },
  });
}
