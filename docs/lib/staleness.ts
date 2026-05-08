const MS_PER_DAY = 1000 * 60 * 60 * 24;

export function isStale(lastVerified: string, maxAgeDays = 30, now = new Date()): boolean {
  const verifiedMs = new Date(lastVerified + 'T00:00:00Z').getTime();
  return now.getTime() - verifiedMs > maxAgeDays * MS_PER_DAY;
}

export function daysSince(lastVerified: string, now = new Date()): number {
  const verifiedMs = new Date(lastVerified + 'T00:00:00Z').getTime();
  return Math.floor((now.getTime() - verifiedMs) / MS_PER_DAY);
}
