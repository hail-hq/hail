import type { MetadataRoute } from 'next';
import { absoluteUrl } from '@/lib/url';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
        // _next/static/ assets are referenced by every page; crawlers don't need to index them.
        disallow: '/_next/',
      },
    ],
    sitemap: absoluteUrl('/sitemap.xml'),
    host: 'https://hail.so',
  };
}
