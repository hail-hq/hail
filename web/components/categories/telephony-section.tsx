'use client';

import type { ColumnDef } from '@tanstack/react-table';
import type { TelephonyNumberRow } from '@/lib/types';
import { CategorySection } from '../category-section';
import { VerifiedCell } from '../verified-cell';
import { priceRange, usd } from '@/lib/format';

const columns: ColumnDef<TelephonyNumberRow>[] = [
  {
    id: 'number',
    accessorKey: 'display_name',
    header: 'Number',
    cell: ({ row }) => (
      <div>
        <div style={{ fontWeight: 700 }}>{row.original.display_name}</div>
        <div style={{ fontSize: 13, marginTop: 2 }}>
          {row.original.country_code} · {row.original.number_type}
        </div>
      </div>
    ),
  },
  {
    id: 'price',
    accessorFn: (row) => Number(row.usd_per_month),
    header: '$/mo (at cost)',
    cell: ({ row }) => usd(row.original.usd_per_month, 2),
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

export function TelephonySection({ data }: { data: TelephonyNumberRow[] }) {
  return (
    <CategorySection<TelephonyNumberRow>
      id="telephony"
      num="04"
      title={
        <>
          <em className="it">Phone</em> numbers
        </>
      }
      count={data.length}
      rangeLabel={priceRange(data.map((r) => r.usd_per_month), 2, 2, 'mo')}
      data={data}
      columns={columns}
      defaultSort={{ id: 'price', desc: false }}
    />
  );
}
