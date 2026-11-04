import type { TTSRow } from './types';

export function langs(l: string[] | string): string {
  return Array.isArray(l) ? l.join(', ') : l;
}

function money(value: string | undefined, symbol: string, fractionDigits: number): string {
  if (value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${symbol}${n.toFixed(fractionDigits)}`;
}

export function usd(value: string | undefined, fractionDigits: number): string {
  return money(value, '$', fractionDigits);
}

/** A row's price in the currency the vendor publishes: USD when it has one,
 * otherwise EUR. The dataset never converts currencies. */
export function price(
  usdValue: string | undefined,
  eurValue: string | undefined,
  fractionDigits: number,
): string {
  if (usdValue !== undefined) return money(usdValue, '$', fractionDigits);
  return money(eurValue, '€', fractionDigits);
}

/** Speech rows priced only in euros. They sit outside the USD price range. */
export function eurOnly(rows: { price_per_minute_usd?: string; price_per_minute_eur?: string }[]): number {
  return rows.filter((r) => r.price_per_minute_usd === undefined && r.price_per_minute_eur !== undefined).length;
}

/** Appended to a USD price range when some rows are priced in euros. */
export function eurNote(count: number): string {
  return count > 0 ? ` · ${count} in EUR` : '';
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
