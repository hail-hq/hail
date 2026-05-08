import type { LLMRow, STTRow, TTSRow } from '@/lib/types';
import { CopyableCode } from './copyable-code';
import { isStale, daysSince } from '@/lib/staleness';
import { url } from '@/lib/url';
import { langs } from '@/lib/format';

type Cell = React.ReactNode;
type CompareRow = { label: string; cells: Cell[]; emphasis?: boolean };

function StaleMaybe({ d }: { d: string }) {
  return isStale(d) ? <span className="stale-pill">stale {daysSince(d)}d</span> : null;
}

function compareUrlWithout(currentIds: string[], idToRemove: string): string {
  const next = currentIds.filter((id) => id !== idToRemove);
  return next.length > 0 ? url(`/costs/compare?m=${next.join(',')}`) : url('/costs/compare');
}

function CompareGrid({
  models,
  currentIds,
  rows,
}: {
  models: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  rows: CompareRow[];
}) {
  return (
    <div className="table-wrap">
      <table className="compare-table">
        <thead>
          <tr>
            <th className="compare-attr"> </th>
            {models.map((m) => (
              <th key={m.model_id} className="compare-model-head">
                <div className="compare-model-head-inner">
                  <div className="compare-model-prov">{m.provider}</div>
                  <div className="compare-model-name">{m.display_name}</div>
                  <CopyableCode value={m.model_id} />
                  <a
                    href={compareUrlWithout(currentIds, m.model_id)}
                    className="compare-remove"
                    title={`Remove ${m.display_name}`}
                    aria-label={`Remove ${m.display_name} from comparison`}
                  >
                    ×
                  </a>
                </div>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.label} className={r.emphasis ? 'compare-row-emphasis' : undefined}>
              <th className="compare-attr-label">{r.label}</th>
              {r.cells.map((c, j) => (
                <td key={models[j]?.model_id ?? j}>{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function LLMCompareTable({ models, currentIds }: { models: LLMRow[]; currentIds: string[] }) {
  const rows: CompareRow[] = [
    { label: 'Output $/MTok', cells: models.map((m) => `$${m.output_per_mtok_usd.toFixed(2)}`), emphasis: true },
    { label: 'Input $/MTok', cells: models.map((m) => `$${m.input_per_mtok_usd.toFixed(2)}`) },
    {
      label: 'Cached input $/MTok',
      cells: models.map((m) =>
        m.cached_input_per_mtok_usd !== undefined ? `$${m.cached_input_per_mtok_usd.toFixed(4)}` : '—',
      ),
    },
    { label: 'Context window', cells: models.map((m) => m.context_window.toLocaleString()) },
    { label: 'Output cap', cells: models.map((m) => m.max_output_tokens.toLocaleString()) },
    {
      label: 'Modalities',
      cells: models.map((m) => `in: ${m.modalities.input.join(', ')} / out: ${m.modalities.output.join(', ')}`),
    },
    { label: 'Tool use', cells: models.map((m) => (m.tool_use ? '✓' : '—')) },
    { label: 'Structured output', cells: models.map((m) => (m.structured_output ? '✓' : '—')) },
    { label: 'Family', cells: models.map((m) => m.model_family ?? '—') },
    { label: 'Knowledge cutoff', cells: models.map((m) => m.knowledge_cutoff ?? '—') },
    {
      label: 'Verified',
      cells: models.map((m) => (
        <span>
          <span className="compare-verified-date">{m.last_verified}</span>
          <StaleMaybe d={m.last_verified} />
        </span>
      )),
    },
    {
      label: 'Source',
      cells: models.map((m) => (
        <a className="compare-source" href={m.source_url} rel="noopener">
          provider page ↗
        </a>
      )),
    },
  ];
  return <CompareGrid models={models} currentIds={currentIds} rows={rows} />;
}

export function STTCompareTable({ models, currentIds }: { models: STTRow[]; currentIds: string[] }) {
  const rows: CompareRow[] = [
    { label: '$/min', cells: models.map((m) => `$${m.price_per_minute_usd.toFixed(6)}`), emphasis: true },
    {
      label: '$/min batch',
      cells: models.map((m) =>
        m.price_per_minute_batch_usd !== undefined ? `$${m.price_per_minute_batch_usd.toFixed(6)}` : '—',
      ),
    },
    { label: 'Streaming', cells: models.map((m) => (m.streaming ? '✓' : '—')) },
    { label: 'Realtime', cells: models.map((m) => (m.realtime ? '✓' : '—')) },
    { label: 'Languages', cells: models.map((m) => langs(m.languages)) },
    { label: 'Diarization', cells: models.map((m) => m.diarization ?? '—') },
    {
      label: 'TTFW',
      cells: models.map((m) => (m.time_to_first_word_ms !== undefined ? `${m.time_to_first_word_ms}ms` : '—')),
    },
    {
      label: 'Verified',
      cells: models.map((m) => (
        <span>
          <span className="compare-verified-date">{m.last_verified}</span>
          <StaleMaybe d={m.last_verified} />
        </span>
      )),
    },
    {
      label: 'Source',
      cells: models.map((m) => (
        <a className="compare-source" href={m.source_url} rel="noopener">
          provider page ↗
        </a>
      )),
    },
  ];
  return <CompareGrid models={models} currentIds={currentIds} rows={rows} />;
}

export function TTSCompareTable({ models, currentIds }: { models: TTSRow[]; currentIds: string[] }) {
  function cloning(m: TTSRow): string {
    if (m.voice_cloning === undefined) return '—';
    if (typeof m.voice_cloning === 'boolean') return m.voice_cloning ? '✓ included' : '—';
    return `$${m.voice_cloning.price_usd} ${m.voice_cloning.unit}`;
  }
  const rows: CompareRow[] = [
    { label: '$/1M chars', cells: models.map((m) => `$${m.price_per_1m_chars_usd.toFixed(2)}`), emphasis: true },
    { label: 'Voice quality', cells: models.map((m) => m.voice_quality) },
    { label: 'Voice cloning', cells: models.map(cloning) },
    { label: 'Voice count', cells: models.map((m) => (m.voice_count !== undefined ? m.voice_count.toLocaleString() : '—')) },
    { label: 'Languages', cells: models.map((m) => langs(m.languages)) },
    { label: 'SSML support', cells: models.map((m) => (m.ssml_support ? '✓' : '—')) },
    {
      label: 'TTFB',
      cells: models.map((m) => (m.time_to_first_byte_ms !== undefined ? `${m.time_to_first_byte_ms}ms` : '—')),
    },
    {
      label: 'Output formats',
      cells: models.map((m) => (m.output_formats ? m.output_formats.join(', ') : '—')),
    },
    {
      label: 'Verified',
      cells: models.map((m) => (
        <span>
          <span className="compare-verified-date">{m.last_verified}</span>
          <StaleMaybe d={m.last_verified} />
        </span>
      )),
    },
    {
      label: 'Source',
      cells: models.map((m) => (
        <a className="compare-source" href={m.source_url} rel="noopener">
          provider page ↗
        </a>
      )),
    },
  ];
  return <CompareGrid models={models} currentIds={currentIds} rows={rows} />;
}
