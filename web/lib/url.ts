export const SITE_ORIGIN =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_SITE_ORIGIN) || 'https://hail.so';

export const siteUrl: URL = new URL(SITE_ORIGIN);

export const MAX_COMPARE = 6;

export function compareHref(ids: string[]): string {
  return ids.length > 0 ? `/costs/compare?m=${ids.join(',')}` : '/costs/compare';
}

export function compareHrefRemove(ids: string[], idToRemove: string): string {
  return compareHref(ids.filter((id) => id !== idToRemove));
}
