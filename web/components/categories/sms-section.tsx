'use client';

import type { ColumnDef } from '@tanstack/react-table';
import type { SmsRateRow } from '@/lib/types';
import { CategorySection } from '../category-section';
import { VerifiedCell } from '../verified-cell';
import { priceRange, usd } from '@/lib/format';

const columns: ColumnDef<SmsRateRow>[] = [
  {
    id: 'provider',
    accessorKey: 'provider',
    header: 'Provider',
    cell: ({ row }) => (
      <div>
        <div style={{ fontWeight: 700 }}>{row.original.provider}</div>
        <div style={{ fontSize: 13, marginTop: 2 }}>
          {row.original.country_code} · {row.original.direction} · long code
        </div>
      </div>
    ),
  },
  {
    id: 'price',
    accessorFn: (row) => Number(row.usd_per_segment),
    header: '$/segment (base)',
    cell: ({ row }) => usd(row.original.usd_per_segment, 5),
    sortingFn: 'basic',
    meta: { num: true, killer: true },
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

export function SmsSection({ data }: { data: SmsRateRow[] }) {
  return (
    <CategorySection<SmsRateRow>
      id="sms"
      num="05"
      title={
        <>
          <em className="it">SMS</em> base rates
        </>
      }
      count={data.length}
      rangeLabel={priceRange(data.map((r) => r.usd_per_segment), 5, 3, 'segment')}
      data={data}
      columns={columns}
      defaultSort={{ id: 'price', desc: false }}
      noun="provider"
    />
  );
}
