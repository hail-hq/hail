"use client";

import type { ColumnDef } from "@tanstack/react-table";
import type { STTRow } from "@/lib/types";
import { CategorySection } from "../category-section";
import { ModelIdCell } from "../model-id-cell";
import { VerifiedCell } from "../verified-cell";
import { eurNote, eurOnly, langs, numOpt, price, priceRange } from "@/lib/format";

const columns: ColumnDef<STTRow>[] = [
  {
    id: "provider",
    accessorKey: "provider",
    header: "Provider",
    cell: ({ row }) => (
      <div>
        <div style={{ fontWeight: 700 }}>{row.original.provider}</div>
        <div style={{ fontSize: 13, marginTop: 2 }}>
          {row.original.display_name}
        </div>
        <ModelIdCell modelId={row.original.model_id} />
      </div>
    ),
  },
  {
    id: "price",
    accessorFn: (row) => numOpt(row.price_per_minute_usd),
    header: "Price/min",
    cell: ({ row }) =>
      price(row.original.price_per_minute_usd, row.original.price_per_minute_eur, 4),
    sortingFn: "basic",
    // Euro rows have no USD value. Keep them last in both directions.
    sortUndefined: "last",
    meta: { num: true, killer: true },
  },
  {
    id: "batch",
    accessorFn: (row) => numOpt(row.price_per_minute_batch_usd),
    header: "Price/min batch",
    cell: ({ row }) =>
      price(row.original.price_per_minute_batch_usd, row.original.price_per_minute_batch_eur, 4),
    sortingFn: "basic",
    // Euro rows have no USD value. Keep them last in both directions.
    sortUndefined: "last",
    meta: { num: true },
  },
  {
    id: "streaming",
    accessorKey: "streaming",
    header: "Streaming",
    cell: ({ row }) => (row.original.streaming ? "✓" : "—"),
  },
  {
    id: "languages",
    accessorKey: "languages",
    header: "Languages",
    cell: ({ row }) => langs(row.original.languages),
  },
  {
    id: "diarization",
    accessorKey: "diarization",
    header: "Diarize",
    cell: ({ row }) => row.original.diarization ?? "—",
  },
  {
    id: "verified",
    accessorKey: "last_verified",
    header: "Verified",
    cell: ({ row }) => <VerifiedCell date={row.original.last_verified} />,
    sortingFn: "alphanumeric",
    meta: { num: true },
  },
];

export function STTSection({ data }: { data: STTRow[] }) {
  return (
    <CategorySection<STTRow>
      id="stt"
      num="02"
      title="Speech to text"
      count={data.length}
      rangeLabel={
        priceRange(
          data.map((r) => r.price_per_minute_usd),
          6,
          4,
          "min",
        ) + eurNote(eurOnly(data))
      }
      data={data}
      columns={columns}
      defaultSort={{ id: "price", desc: false }}
    />
  );
}
