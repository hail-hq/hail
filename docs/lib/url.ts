export const BASE_PATH = '/docs';

export const SITE_ORIGIN =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_SITE_ORIGIN) || 'https://hail.so';

// Use this for plain <a href> and route-handler URLs; <Link> from next/link
// already prepends basePath on its own.
export function url(path: string): string {
  if (
    path.startsWith('http://') ||
    path.startsWith('https://') ||
    path.startsWith('mailto:') ||
    path.startsWith('//')
  ) {
    return path;
  }
  const normalized = path.startsWith('/') ? path : `/${path}`;
  return `${BASE_PATH}${normalized}`;
}

export function absoluteUrl(path: string): string {
  return `${SITE_ORIGIN}${url(path)}`;
}
