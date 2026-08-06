export const SITE_ORIGIN =
  (typeof process !== "undefined" && process.env.NEXT_PUBLIC_SITE_ORIGIN) ||
  "https://hail.so";

export const siteUrl: URL = new URL(SITE_ORIGIN);

/**
 * Absolute URL onto the main site. This app is served both behind hail.so's
 * `/costs/*` rewrite and on its own Vercel domain, so cross-site chrome links
 * must be absolute — a relative `/pricing` 404s on the bare domain.
 */
export function siteHref(path: string): string {
  return new URL(path, siteUrl).toString();
}

export const MAX_COMPARE = 6;

export function compareHref(ids: string[]): string {
  return ids.length > 0
    ? `/costs/compare?m=${ids.join(",")}`
    : "/costs/compare";
}

export function compareHrefRemove(ids: string[], idToRemove: string): string {
  return compareHref(ids.filter((id) => id !== idToRemove));
}
