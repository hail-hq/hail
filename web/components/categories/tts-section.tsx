"use client";

import type { ColumnDef } from "@tanstack/react-table";
import type { TTSRow } from "@/lib/types";
import { CategorySection } from "../category-section";
import { ModelIdCell } from "../model-id-cell";
import { VerifiedCell } from "../verified-cell";
import { formatCloning, langs, rowRange, ttsPrice, usdSort } from "@/lib/format";

const columns: ColumnDef<TTSRow>[] = [
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
        {row.original.api_endpoint ? (
          <code>{new URL(row.original.api_endpoint).pathname}</code>
        ) : (
          <ModelIdCell modelId={row.original.model_id} />
        )}
      </div>
    ),
  },
  {
    id: "price",
    accessorFn: (row) => usdSort(row, "price_per_1m_chars_usd"),
    header: "Price (sort by $/1M characters)",
    cell: ({ row }) => ttsPrice(row.original),
    sortingFn: "basic",
    sortUndefined: "last",
    meta: { num: true, killer: true },
  },
  {
    id: "quality",
    accessorKey: "voice_quality",
    header: "Quality",
    cell: ({ row }) => row.original.voice_quality,
  },
  {
    id: "cloning",
    accessorKey: "voice_cloning",
    header: "Cloning",
    cell: ({ row }) => formatCloning(row.original.voice_cloning, "✓"),
  },
  {
    id: "languages",
    accessorKey: "languages",
    header: "Languages",
    cell: ({ row }) => langs(row.original.languages),
  },
  {
    id: "ttfb",
    accessorKey: "time_to_first_byte_ms",
    header: "TTFB",
    cell: ({ row }) =>
      row.original.time_to_first_byte_ms !== undefined
        ? `${row.original.time_to_first_byte_ms}ms`
        : "—",
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

export function TTSSection({ data }: { data: TTSRow[] }) {
  return (
    <CategorySection<TTSRow>
      id="tts"
      num="03"
      title="Text to speech"
      count={data.length}
      rangeLabel={rowRange(
        data,
        "price_per_1m_chars_usd",
        2,
        2,
        "1M chars",
      )}
      data={data}
      columns={columns}
      defaultSort={{ id: "price", desc: false }}
    />
  );
}
