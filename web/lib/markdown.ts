import 'server-only';
import type { LLMRow, STTRow, TTSRow, CostsFile } from './types';
import { langs, num, mostRecent, usd } from './format';

function tableRow(cells: (string | number | undefined)[]): string {
  return `| ${cells.map((c) => (c === undefined || c === null ? '—' : String(c))).join(' | ')} |`;
}

function llmTable(rows: LLMRow[]): string {
  const header = tableRow([
    'Provider',
    'Model',
    'Output $/MTok',
    'Input $/MTok',
    'Cached $/MTok',
    'Context',
    'Out cap',
    'Tools',
    'Modalities',
    'Verified',
    'Source',
  ]);
  const sep = tableRow(Array(11).fill('---'));
  const body = rows.map((r) =>
    tableRow([
      r.provider,
      `${r.display_name} (\`${r.model_id}\`)`,
      usd(r.output_per_mtok_usd, 2),
      usd(r.input_per_mtok_usd, 2),
      usd(r.cache_read_per_mtok_usd, 4),
      r.context_window.toLocaleString(),
      r.max_output_tokens.toLocaleString(),
      r.supports_tool_use ? 'yes' : 'no',
      `in: ${r.modalities.input.join(',')} / out: ${r.modalities.output.join(',')}`,
      r.last_verified,
      `[link](${r.source_url})`,
    ]),
  );
  return [header, sep, ...body].join('\n');
}

function sttTable(rows: STTRow[]): string {
  const header = tableRow([
    'Provider',
    'Model',
    '$/min',
    '$/min batch',
    'Streaming',
    'Realtime',
    'Languages',
    'Diarization',
    'Verified',
    'Source',
  ]);
  const sep = tableRow(Array(10).fill('---'));
  const body = rows.map((r) =>
    tableRow([
      r.provider,
      `${r.display_name} (\`${r.model_id}\`)`,
      usd(r.price_per_minute_usd, 6),
      usd(r.price_per_minute_batch_usd, 6),
      r.streaming ? 'yes' : 'no',
      r.realtime ? 'yes' : 'no',
      langs(r.languages),
      r.diarization ?? '—',
      r.last_verified,
      `[link](${r.source_url})`,
    ]),
  );
  return [header, sep, ...body].join('\n');
}

function ttsTable(rows: TTSRow[]): string {
  const header = tableRow([
    'Provider',
    'Model',
    '$/1M chars',
    'Quality',
    'Cloning',
    'Languages',
    'TTFB',
    'SSML',
    'Verified',
    'Source',
  ]);
  const sep = tableRow(Array(10).fill('---'));
  const body = rows.map((r) => {
    const cloning =
      r.voice_cloning === undefined
        ? '—'
        : typeof r.voice_cloning === 'boolean'
          ? r.voice_cloning
            ? 'yes'
            : 'no'
          : `$${r.voice_cloning.price_usd} ${r.voice_cloning.unit}`;
    return tableRow([
      r.provider,
      `${r.display_name} (\`${r.model_id}\`)`,
      usd(r.price_per_1m_chars_usd, 2),
      r.voice_quality,
      cloning,
      langs(r.languages),
      r.time_to_first_byte_ms !== undefined ? `${r.time_to_first_byte_ms}ms` : '—',
      r.ssml_supported ? 'yes' : 'no',
      r.last_verified,
      `[link](${r.source_url})`,
    ]);
  });
  return [header, sep, ...body].join('\n');
}

function notesBlock<T extends { provider: string; display_name: string; notes?: string }>(
  rows: T[],
): string {
  const withNotes = rows.filter((r) => r.notes && r.notes.trim().length > 0);
  if (withNotes.length === 0) return '';
  return [
    '',
    '**Notes:**',
    '',
    ...withNotes.map((r) => `- **${r.provider} ${r.display_name}** — ${r.notes}`),
    '',
  ].join('\n');
}

export function renderCostsMarkdown(
  llm: CostsFile<LLMRow>,
  stt: CostsFile<STTRow>,
  tts: CostsFile<TTSRow>,
  generatedAt: string,
): string {
  const totalModels = llm.models.length + stt.models.length + tts.models.length;
  const providers = new Set<string>();
  for (const m of [...llm.models, ...stt.models, ...tts.models]) providers.add(m.provider);

  const llmOuts = llm.models.map((m) => num(m.output_per_mtok_usd));
  const sttPrices = stt.models.map((m) => num(m.price_per_minute_usd));
  const ttsPrices = tts.models.map((m) => num(m.price_per_1m_chars_usd));

  const verified = mostRecent(llm.models, stt.models, tts.models);

  return `---
title: Model costs
description: Public, validated pricing and capability data for AI model providers (LLMs, STT, TTS).
license: CC-BY-4.0
version: 2
verified: ${verified}
generated_at: ${generatedAt}
source: https://github.com/hail-hq/hail/tree/main/costs
---

# Model costs

Public, validated pricing and capability data for AI model providers — large language models, speech-to-text, and text-to-speech. Schema-validated, dual-licensed CC-BY-4.0 for free reuse.

## At a glance

- **Models:** ${totalModels} (${llm.models.length} LLM · ${stt.models.length} STT · ${tts.models.length} TTS)
- **Providers:** ${providers.size}
- **LLM output range:** $${Math.min(...llmOuts).toFixed(2)} – $${Math.max(...llmOuts).toFixed(2)} / Mtok
- **STT range:** $${Math.min(...sttPrices).toFixed(6)} – $${Math.max(...sttPrices).toFixed(4)} / min
- **TTS range:** $${Math.min(...ttsPrices).toFixed(2)} – $${Math.max(...ttsPrices).toFixed(2)} / 1M chars
- **Verified:** ${verified}

## For agents

Fetch the raw JSON for programmatic use — this is the source of truth, schema-validated on every PR:

- LLMs: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json>
- STT: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json>
- TTS: <https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json>

JSON Schemas: <https://github.com/hail-hq/hail/tree/main/costs/schema>

## Large language models

${llmTable(llm.models)}
${notesBlock(llm.models)}

## Speech-to-text

${sttTable(stt.models)}
${notesBlock(stt.models)}

## Text-to-speech

${ttsTable(tts.models)}
${notesBlock(tts.models)}

---

_This page is generated from the JSON datasets at ${generatedAt}. Verify any price against the provider's official pricing page before billing decisions; dataset is updated via [GitHub PRs](https://github.com/hail-hq/hail/tree/main/costs)._
`;
}
