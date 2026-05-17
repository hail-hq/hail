export function langs(l: string[] | string): string {
  return Array.isArray(l) ? l.join(', ') : l;
}

export function usd(value: string | undefined, fractionDigits: number): string {
  if (value === undefined) return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `$${n.toFixed(fractionDigits)}`;
}

export function num(value: string): number {
  return Number(value);
}

export function mostRecent(...groups: { last_verified: string }[][]): string {
  let max = '';
  for (const group of groups) {
    for (const row of group) {
      if (row.last_verified > max) max = row.last_verified;
    }
  }
  return max;
}
