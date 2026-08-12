"use client";

import type { ColumnDef } from "@tanstack/react-table";
import type { LLMRow } from "@/lib/types";
import { CategorySection } from "../category-section";
import { ModelIdCell } from "../model-id-cell";
import { VerifiedCell } from "../verified-cell";
import { num, numOpt, priceRange, usd } from "@/lib/format";

const columns: ColumnDef<LLMRow>[] = [
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
    id: "output",
    accessorFn: (row) => num(row.output_per_mtok_usd),
    header: "Output $/MTok",
    cell: ({ row }) => usd(row.original.output_per_mtok_usd, 2),
    sortingFn: "basic",
    meta: { num: true, killer: true },
  },
  {
    id: "input",
    accessorFn: (row) => num(row.input_per_mtok_usd),
    header: "Input $/MTok",
    cell: ({ row }) => usd(row.original.input_per_mtok_usd, 2),
    sortingFn: "basic",
    meta: { num: true },
  },
  {
    id: "cached",
    accessorFn: (row) => numOpt(row.cache_read_per_mtok_usd),
    header: "Cached $/MTok",
    cell: ({ row }) => usd(row.original.cache_read_per_mtok_usd, 4),
    sortingFn: "basic",
    meta: { num: true },
  },
  {
    id: "context",
    accessorKey: "context_window",
    header: "Context",
    cell: ({ row }) => row.original.context_window.toLocaleString(),
    sortingFn: "basic",
    meta: { num: true },
  },
  {
    id: "max_out",
    accessorKey: "max_output_tokens",
    header: "Out cap",
    cell: ({ row }) => row.original.max_output_tokens.toLocaleString(),
    sortingFn: "basic",
    meta: { num: true },
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

export function LLMSection({ data }: { data: LLMRow[] }) {
  return (
    <CategorySection<LLMRow>
      id="llm"
      num="01"
      title="Large language models"
      count={data.length}
      rangeLabel={priceRange(
        data.map((r) => r.output_per_mtok_usd),
        2,
        2,
        "Mtok output",
      )}
      data={data}
      columns={columns}
      defaultSort={{ id: "output", desc: false }}
    />
  );
}
