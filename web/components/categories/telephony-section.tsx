"use client";

import type { ColumnDef } from "@tanstack/react-table";
import type { NumberRow } from "@/lib/types";
import { CategorySection } from "../category-section";
import { VerifiedCell } from "../verified-cell";
import { priceRange, usd } from "@/lib/format";

const PROVIDER_LABEL: Record<NumberRow["provider"], string> = { twilio: "Twilio", telnyx: "Telnyx", didww: "DIDWW" };

const columns: ColumnDef<NumberRow>[] = [
  {
    id: "number",
    accessorKey: "display_name",
    header: "Number",
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
    id: "provider",
    accessorKey: "provider",
    header: "Carrier",
    cell: ({ row }) => PROVIDER_LABEL[row.original.provider] ?? row.original.provider,
  },
  {
    id: "price",
    accessorFn: (row) => Number(row.usd_per_month),
    header: "$/mo (at cost)",
    cell: ({ row }) => usd(row.original.usd_per_month, 2),
    sortingFn: "basic",
    meta: { num: true, killer: true },
  },
  {
    id: "calls",
    header: "Calls",
    accessorKey: "voice",
    cell: ({ row }) => (row.original.voice ? "✓" : "—"),
    meta: { num: true },
  },
  {
    id: "texts",
    header: "Texts",
    accessorKey: "sms",
    cell: ({ row }) => (row.original.sms ? "✓" : "—"),
    meta: { num: true },
  },
  {
    id: "mms",
    header: "MMS",
    accessorKey: "mms",
    cell: ({ row }) => (row.original.mms ? "✓" : "—"),
    meta: { num: true },
  },
  {
    id: "verify",
    header: "Verify first",
    accessorKey: "verification_required",
    cell: ({ row }) => (row.original.verification_required ? "yes" : "—"),
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

export function TelephonySection({ data }: { data: NumberRow[] }) {
  return (
    <CategorySection<NumberRow>
      id="telephony"
      num="04"
      title="Phone numbers"
      count={data.length}
      rangeLabel={priceRange(
        data.map((r) => r.usd_per_month),
        2,
        2,
        "mo",
      )}
      data={data}
      columns={columns}
      defaultSort={{ id: "price", desc: false }}
      noun="number"
    />
  );
}
