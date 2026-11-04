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

/** Prices are USD fields (`*_usd`, `usd_*`) unless the vendor publishes no
 * USD price. Then the row uses the matching EUR fields (`*_eur`, `eur_*`)
 * instead. Prices are never converted, so EUR rows keep their own symbol,
 * sort last, and stay out of USD price ranges. Callers name the USD field. */
type Row = object;

function eurField(usdField: string): string {
  if (usdField.endsWith('_usd')) return `${usdField.slice(0, -4)}_eur`;
  if (usdField.startsWith('usd_')) return `eur_${usdField.slice(4)}`;
  return usdField;
}

function field(row: Row, name: string): string | undefined {
  const v = (row as Record<string, unknown>)[name];
  return typeof v === 'string' ? v : undefined;
}

/** The row's currency, from which price fields it uses. */
export function currencyOf(row: Row): 'USD' | 'EUR' {
  return Object.keys(row).some((k) => k.endsWith('_eur') || k.startsWith('eur_')) ? 'EUR' : 'USD';
}

export function isUsd(row: Row): boolean {
  return currencyOf(row) === 'USD';
}

/** The amount for a USD field name, read from the EUR field on EUR rows. */
export function amount(row: Row, usdField: string): string | undefined {
  return isUsd(row) ? field(row, usdField) : field(row, eurField(usdField));
}

/** A price in the row's own currency. */
export function rowPrice(row: Row, usdField: string, fractionDigits: number): string {
  return money(amount(row, usdField), currencyOf(row), fractionDigits);
}

/** Sort value: USD rows only. EUR rows sort last. */
export function usdSort(row: Row, usdField: string): number | undefined {
  return isUsd(row) ? numOpt(field(row, usdField)) : undefined;
}

/** The USD price range of some rows, with a count of EUR rows. A set with
 * only EUR rows shows its euro range. */
export function rowRange(
  rows: Row[],
  usdField: string,
  minDigits: number,
  maxDigits: number,
  suffix: string,
): string {
  const usdRows = rows.filter(isUsd);
  const eurRows = rows.filter((r) => !isUsd(r));
  if (usdRows.length === 0 && eurRows.length > 0) {
    return priceRange(eurRows.map((r) => amount(r, usdField)), minDigits, maxDigits, suffix, '€');
  }
  const range = priceRange(usdRows.map((r) => field(r, usdField)), minDigits, maxDigits, suffix);
  return eurRows.length === 0 ? range : `${range} · ${eurRows.length} in EUR`;
}

/** A price column label. One currency keeps its symbol, for example "$/min"
 * or "€/min". Mixed currencies say so, because nothing is converted. */
export function priceLabel(rows: Row[], usdLabel: string, plainLabel: string): string {
  const currencies = new Set(rows.map(currencyOf));
  if (currencies.size <= 1 && !currencies.has('EUR')) return usdLabel;
  if (currencies.size === 1) return usdLabel.replace('$', '€');
  return `${plainLabel} (mixed currencies, not converted)`;
}

/** The rows a table holds, for header labels that depend on them. */
export function rowsOf<T>(table: { getCoreRowModel: () => { rows: { original: T }[] } }): T[] {
  return table.getCoreRowModel().rows.map((r) => r.original);
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
): string {
  if (vc === undefined) return '—';
  if (typeof vc === 'boolean') return vc ? includedLabel : '—';
  const eurPrice = (vc as { price_eur?: string }).price_eur;
  return eurPrice !== undefined
    ? `${money(eurPrice, 'EUR', 2)} ${vc.unit}`
    : `${money(vc.price_usd, 'USD', 2)} ${vc.unit}`;
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
