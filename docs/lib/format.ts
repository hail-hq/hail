export function langs(l: string[] | string): string {
  return Array.isArray(l) ? l.join(', ') : l;
}
