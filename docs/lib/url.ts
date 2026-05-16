export const SITE_ORIGIN =
  (typeof process !== 'undefined' && process.env.NEXT_PUBLIC_SITE_ORIGIN) || 'https://hail.so';

export const siteUrl: URL = new URL(SITE_ORIGIN);
