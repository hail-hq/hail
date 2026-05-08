'use client';

import { useState } from 'react';
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table';

export interface CategorySectionProps<T> {
  id: string;
  num: string;
  title: React.ReactNode;
  count: number;
  rangeLabel: string;
  data: T[];
  columns: ColumnDef<T>[];
  defaultSort?: { id: string; desc: boolean };
}

export function CategorySection<T extends { provider: string }>({
  id,
  num,
  title,
  count,
  rangeLabel,
  data,
  columns,
  defaultSort,
}: CategorySectionProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(defaultSort ? [defaultSort] : []);

  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
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
            {count} {count === 1 ? 'model' : 'models'} · {rangeLabel}
          </span>
        </div>

        <div className="table-wrap">
          <table className="wire-table">
            <thead>
              {table.getHeaderGroups().map((hg) => (
                <tr key={hg.id}>
                  {hg.headers.map((h) => {
                    const sorted = h.column.getIsSorted();
                    const meta = h.column.columnDef.meta as
                      | { num?: boolean; killer?: boolean }
                      | undefined;
                    const cls = [meta?.num ? 'num' : '', meta?.killer ? 'killer' : '']
                      .filter(Boolean)
                      .join(' ');
                    const ariaSort: 'ascending' | 'descending' | undefined =
                      sorted === 'asc' ? 'ascending' : sorted === 'desc' ? 'descending' : undefined;
                    return (
                      <th
                        key={h.id}
                        className={cls || undefined}
                        aria-sort={ariaSort}
                        onClick={h.column.getToggleSortingHandler()}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' || e.key === ' ') {
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
                    const cls = [meta?.num ? 'num' : '', meta?.killer ? 'killer' : '']
                      .filter(Boolean)
                      .join(' ');
                    return (
                      <td key={cell.id} className={cls || undefined}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
