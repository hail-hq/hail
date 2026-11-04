import type { TTSRow } from './types';

export function langs(l: string[] | string): string {
  return Array.isArray(l) ? l.join(', ') : l;
}

const SYMBOL: Record<string, string> = { USD: '$', EUR: '€' };

function money(value: string | undefined, currency: string, fractionDigits: number): string {
  if (value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${SYMBOL[currency] ?? `${currency} `}${n.toFixed(fractionDigits)}`;
}

export function usd(value: string | undefined, fractionDigits: number): string {
  return money(value, 'USD', fractionDigits);
}

/** A row priced in another currency keeps the vendor's figures. Prices are
 * never converted, so such rows are shown with their own symbol, sort last,
 * and stay out of USD price ranges. */
export type Priced = { currency?: string };

export function isUsd(row: Priced): boolean {
  return row.currency === undefined || row.currency === 'USD';
}

/** A price in the row's own currency. */
export function rowPrice(row: Priced, value: string | undefined, fractionDigits: number): string {
  return money(value, row.currency ?? 'USD', fractionDigits);
}

/** Sort value: USD rows only. Other currencies sort last. */
export function usdSort(row: Priced, value: string | undefined): number | undefined {
  return isUsd(row) ? numOpt(value) : undefined;
}

/** The USD price range of some rows, with a count of rows in other currencies. */
export function rowRange<T extends Priced>(
  rows: T[],
  value: (row: T) => string | undefined,
  minDigits: number,
  maxDigits: number,
  suffix: string,
): string {
  const usdRows = rows.filter(isUsd);
  const other = rows.filter((r) => !isUsd(r));
  const currencies = [...new Set(other.map((r) => r.currency ?? 'USD'))];
  // No USD rows but one other currency: show that currency's own range.
  if (usdRows.length === 0 && currencies.length === 1) {
    return priceRange(other.map(value), minDigits, maxDigits, suffix, symbolOf(currencies[0]));
  }
  const range = priceRange(usdRows.map(value), minDigits, maxDigits, suffix);
  if (other.length === 0) return range;
  return `${range} · ${other.length} in ${currencies.join(', ')}`;
}

/** A price column label. One currency keeps its symbol, for example "$/min"
 * or "€/min". Mixed currencies say so, because nothing is converted. */
export function priceLabel(rows: Priced[], usdLabel: string, plainLabel: string): string {
  const currencies = new Set(rows.map((r) => r.currency ?? 'USD'));
  if (currencies.size === 0 || (currencies.size === 1 && currencies.has('USD'))) return usdLabel;
  if (currencies.size === 1) return usdLabel.replace('$', symbolOf([...currencies][0]));
  return `${plainLabel} (mixed currencies, not converted)`;
}

/** The rows a table holds, for header labels that depend on them. */
export function rowsOf<T>(table: { getCoreRowModel: () => { rows: { original: T }[] } }): T[] {
  return table.getCoreRowModel().rows.map((r) => r.original);
}

function symbolOf(currency: string): string {
  return SYMBOL[currency] ?? `${currency} `;
}

export function num(value: string): number {
  return Number(value);
}

export function numOpt(value: string | undefined): number | undefined {
  return value === undefined ? undefined : Number(value);
}

export function priceRange(
  values: (string | undefined)[],
  minDigits: number,
  maxDigits: number,
  suffix: string,
  symbol = '$',
): string {
  let min = Infinity;
  let max = -Infinity;
  for (const v of values) {
    if (v === undefined) continue;
    const n = Number(v);
    if (!Number.isFinite(n)) continue;
    if (n < min) min = n;
    if (n > max) max = n;
  }
  if (!Number.isFinite(min)) return '—';
  return `${symbol}${min.toFixed(minDigits)} – ${symbol}${max.toFixed(maxDigits)} / ${suffix}`;
}

export function formatCloning(
  vc: TTSRow['voice_cloning'],
  includedLabel: string,
  currency = 'USD',
): string {
  if (vc === undefined) return '—';
  if (typeof vc === 'boolean') return vc ? includedLabel : '—';
  return `${money(vc.price_usd, currency, 2)} ${vc.unit}`;
}

export function mostRecent(...groups: { last_verified: string }[][]): string {
  let max = '';
  for (const group of groups) {
    for (const row of group) {
      if (row.last_verified > max) max = row.last_verified;
    }
  }
  return max || '—';
}
