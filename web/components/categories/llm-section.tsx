'use client';

import type { ColumnDef } from '@tanstack/react-table';
import type { LLMRow } from '@/lib/types';
import { CategorySection } from '../category-section';
import { ModelIdCell } from '../model-id-cell';
import { VerifiedCell } from '../verified-cell';

const columns: ColumnDef<LLMRow>[] = [
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
    id: 'output',
    accessorKey: 'output_per_mtok_usd',
    header: 'Output $/MTok',
    cell: ({ row }) => `$${row.original.output_per_mtok_usd.toFixed(2)}`,
    sortingFn: 'basic',
    meta: { num: true, killer: true },
  },
  {
    id: 'input',
    accessorKey: 'input_per_mtok_usd',
    header: 'Input $/MTok',
    cell: ({ row }) => `$${row.original.input_per_mtok_usd.toFixed(2)}`,
    sortingFn: 'basic',
    meta: { num: true },
  },
  {
    id: 'cached',
    accessorKey: 'cached_input_per_mtok_usd',
    header: 'Cached $/MTok',
    cell: ({ row }) =>
      row.original.cached_input_per_mtok_usd !== undefined
        ? `$${row.original.cached_input_per_mtok_usd.toFixed(4)}`
        : '—',
    sortingFn: 'basic',
    meta: { num: true },
  },
  {
    id: 'context',
    accessorKey: 'context_window',
    header: 'Context',
    cell: ({ row }) => row.original.context_window.toLocaleString(),
    sortingFn: 'basic',
    meta: { num: true },
  },
  {
    id: 'max_out',
    accessorKey: 'max_output_tokens',
    header: 'Out cap',
    cell: ({ row }) => row.original.max_output_tokens.toLocaleString(),
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

function priceRange(rows: LLMRow[]): string {
  if (rows.length === 0) return '—';
  const outs = rows.map((r) => r.output_per_mtok_usd);
  return `$${Math.min(...outs).toFixed(2)} – $${Math.max(...outs).toFixed(2)} / Mtok output`;
}

export function LLMSection({ data }: { data: LLMRow[] }) {
  return (
    <CategorySection<LLMRow>
      id="llm"
      num="01"
      title={
        <>
          Large language <em className="it">models</em>
        </>
      }
      count={data.length}
      rangeLabel={priceRange(data)}
      data={data}
      columns={columns}
      defaultSort={{ id: 'output', desc: false }}
    />
  );
}
