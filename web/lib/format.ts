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
  const range = priceRange(rows.filter(isUsd).map(value), minDigits, maxDigits, suffix);
  const other = rows.filter((r) => !isUsd(r));
  if (other.length === 0) return range;
  const currencies = [...new Set(other.map((r) => r.currency))].join(', ');
  return `${range} · ${other.length} in ${currencies}`;
}

/** A compare row label: a pair that mixes currencies says so. */
export function priceLabel(rows: Priced[], usdLabel: string, plainLabel: string): string {
  return rows.every(isUsd) ? usdLabel : `${plainLabel} (mixed currencies, not converted)`;
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
  return `$${min.toFixed(minDigits)} – $${max.toFixed(maxDigits)} / ${suffix}`;
}

export function formatCloning(
  vc: TTSRow['voice_cloning'],
  includedLabel: string,
): string {
  if (vc === undefined) return '—';
  if (typeof vc === 'boolean') return vc ? includedLabel : '—';
  return `${usd(vc.price_usd, 2)} ${vc.unit}`;
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
