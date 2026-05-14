import type { MetadataRoute } from 'next';
import { absoluteUrl } from '@/lib/url';

export default function robots(): MetadataRoute.Robots {
  return {
    rules: [
      {
        userAgent: '*',
        allow: '/',
        // _next/static/ assets are referenced by every page; crawlers don't need to index them.
        // /costs/compare is a dynamic SSR route whose own links generate a combinatorial
        // explosion of ?m=… variants (a crawler trap). The data is already crawlable via
        // /costs, /costs.md, and the raw JSON on GitHub.
        disallow: ['/_next/', '/costs/compare'],
      },
    ],
    sitemap: absoluteUrl('/sitemap.xml'),
    host: 'https://hail.so',
  };
}
