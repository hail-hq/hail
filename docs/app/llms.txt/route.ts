import { llm, stt, tts } from '@/lib/costs';
import { source } from '@/lib/source';
import { absoluteUrl } from '@/lib/url';

export const dynamic = 'force-static';

export function GET() {
  const totalModels = llm.models.length + stt.models.length + tts.models.length;

  const proseEntries = source
    .getPages()
    .map((page) => `- [${page.data.title}](${absoluteUrl(page.url)}): ${page.data.description}`)
    .join('\n');

  const body = `# Hail

> Universal communication platform for AI agents — outbound phone calls, SMS, and email. Self-hostable, MCP-native, AGPLv3.

The docs and pricing data are designed to be agent-readable. Resources are listed below in the format that works best for programmatic consumption. The full content of every prose page is also available at [/llms-full.txt](${absoluteUrl('/llms-full.txt')}).

## Documentation

${proseEntries}

## Pricing data

The Hail costs dataset tracks ${totalModels} models across LLM, STT, and TTS providers. Schema-validated, dual-licensed CC-BY-4.0 for embedding in third-party tools.

- [Model costs (markdown)](${absoluteUrl('/costs.md')}): Full costs report with summary stats, three tables (LLMs, STT, TTS), and per-row source links. ${totalModels} models, refreshed weekly.
- [Model costs (HTML)](${absoluteUrl('/costs')}): Interactive sortable view with provider filters and side-by-side compare.
- [LLM costs (JSON)](https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json): ${llm.models.length} large language models with input/output/cached pricing per million tokens, context window, modalities, tool-use support.
- [STT costs (JSON)](https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json): ${stt.models.length} speech-to-text models with per-minute pricing, language coverage, streaming support.
- [TTS costs (JSON)](https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json): ${tts.models.length} text-to-speech models with per-1M-character pricing, voice quality, cloning support.
- [JSON Schemas](${absoluteUrl('/costs/schema/llm.json')}): Draft 2020-12 schemas for the three datasets above (also stt.json, tts.json under the same path).

## Source

- [GitHub repository](https://github.com/hail-hq/hail): Full source. AGPLv3 (code) / CC-BY-4.0 (costs/ dataset).

## Optional

- [Hail website](https://hail.so): Marketing landing and console.
`;

  return new Response(body, {
    status: 200,
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'public, max-age=0, s-maxage=600, stale-while-revalidate=86400',
    },
  });
}
