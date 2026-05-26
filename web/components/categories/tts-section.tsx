'use client';

import type { ColumnDef } from '@tanstack/react-table';
import type { TTSRow } from '@/lib/types';
import { CategorySection } from '../category-section';
import { ModelIdCell } from '../model-id-cell';
import { VerifiedCell } from '../verified-cell';
import { formatCloning, langs, numOpt, priceRange, usd } from '@/lib/format';

const columns: ColumnDef<TTSRow>[] = [
  {
    id: 'provider',
    accessorKey: 'provider',
    header: 'Provider',
    cell: ({ row }) => (
      <div>
        <div style={{ fontWeight: 700 }}>{row.original.provider}</div>
        <div style={{ fontSize: 13, marginTop: 2 }}>{row.original.display_name}</div>
        <ModelIdCell modelId={row.original.model_id} />
      </div>
    ),
  },
  {
    id: 'price',
    accessorFn: (row) => numOpt(row.price_per_1m_chars_usd),
    header: '$/1M chars',
    cell: ({ row }) => usd(row.original.price_per_1m_chars_usd, 2),
    sortingFn: 'basic',
    meta: { num: true, killer: true },
  },
  {
    id: 'quality',
    accessorKey: 'voice_quality',
    header: 'Quality',
    cell: ({ row }) => row.original.voice_quality,
  },
  {
    id: 'cloning',
    accessorKey: 'voice_cloning',
    header: 'Cloning',
    cell: ({ row }) => formatCloning(row.original.voice_cloning, '✓'),
  },
  {
    id: 'languages',
    accessorKey: 'languages',
    header: 'Languages',
    cell: ({ row }) => langs(row.original.languages),
  },
  {
    id: 'ttfb',
    accessorKey: 'time_to_first_byte_ms',
    header: 'TTFB',
    cell: ({ row }) =>
      row.original.time_to_first_byte_ms !== undefined
        ? `${row.original.time_to_first_byte_ms}ms`
        : '—',
    sortingFn: 'basic',
    meta: { num: true },
  },
  {
    id: 'verified',
    accessorKey: 'last_verified',
    header: 'Verified',
    cell: ({ row }) => <VerifiedCell date={row.original.last_verified} />,
    sortingFn: 'alphanumeric',
    meta: { num: true },
  },
];

export function TTSSection({ data }: { data: TTSRow[] }) {
  return (
    <CategorySection<TTSRow>
      id="tts"
      num="03"
      title={
        <>
          Text to <em className="it">speech</em>
        </>
      }
      count={data.length}
      rangeLabel={priceRange(data.map((r) => r.price_per_1m_chars_usd), 2, 2, '1M chars')}
      data={data}
      columns={columns}
      defaultSort={{ id: 'price', desc: false }}
    />
  );
}
