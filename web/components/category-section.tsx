"use client";

import { useCallback, useState } from "react";
import {
  type ColumnDef,
  type SortingState,
  type Updater,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import posthog from "posthog-js";
import { TableWrap } from "./table-wrap";

export interface CategorySectionProps<T> {
  id: string;
  num: string;
  title: React.ReactNode;
  count: number;
  rangeLabel: string;
  data: T[];
  columns: ColumnDef<T>[];
  defaultSort?: { id: string; desc: boolean };
  noun?: string;
}

export function CategorySection<T>({
  id,
  num,
  title,
  count,
  rangeLabel,
  data,
  columns,
  defaultSort,
  noun = "model",
}: CategorySectionProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(
    defaultSort ? [defaultSort] : [],
  );

  const handleSortingChange = useCallback(
    (updater: Updater<SortingState>) => {
      setSorting((prev) => {
        const next = typeof updater === "function" ? updater(prev) : updater;
        if (next.length > 0) {
          posthog.capture("table_sorted", {
            category: id,
            column: next[0].id,
            direction: next[0].desc ? "desc" : "asc",
          });
        }
        return next;
      });
    },
    [id],
  );

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: handleSortingChange,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <section className="cat" id={id}>
      <div className="wrap">
        <div className="cat-bar">
          <span className="num">{num}</span>
          <h2>{title}</h2>
          <span className="count">
            {count} {count === 1 ? noun : `${noun}s`} · {rangeLabel}
          </span>
        </div>

        <TableWrap>
          <table className="wire-table">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((h) => {
                    const sorted = h.column.getIsSorted();
                    const meta = h.column.columnDef.meta as
                      | { num?: boolean; killer?: boolean }
                      | undefined;
                    const cls = [
                      meta?.num ? "num" : "",
                      meta?.killer ? "killer" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");
                    const ariaSort: "ascending" | "descending" | undefined =
                      sorted === "asc"
                        ? "ascending"
                        : sorted === "desc"
                          ? "descending"
                          : undefined;
                    return (
                      <th
                        key={h.id}
                        className={cls || undefined}
                        aria-sort={ariaSort}
                        onClick={h.column.getToggleSortingHandler()}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            h.column.toggleSorting();
                          }
                        }}
                        tabIndex={0}
                        role="columnheader"
                      >
                        {flexRender(h.column.columnDef.header, h.getContext())}
                      </th>
                    );
                  })}
                </tr>
              ))}
            </thead>
            <tbody>
              {table.getRowModel().rows.map((row) => (
                <tr key={row.id}>
                  {row.getVisibleCells().map((cell) => {
                    const meta = cell.column.columnDef.meta as
                      | { num?: boolean; killer?: boolean }
                      | undefined;
                    const cls = [
                      meta?.num ? "num" : "",
                      meta?.killer ? "killer" : "",
                    ]
                      .filter(Boolean)
                      .join(" ");
                    return (
                      <td key={cell.id} className={cls || undefined}>
                        {flexRender(
                          cell.column.columnDef.cell,
                          cell.getContext(),
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </div>
    </section>
  );
}
