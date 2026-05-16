import type { MetadataRoute } from 'next';
import { llm, stt, tts } from '@/lib/costs';
import { source } from '@/lib/source';
import { absoluteUrl } from '@/lib/url';

export default function sitemap(): MetadataRoute.Sitemap {
  // Costs dataset shares `updated` across all three files (refreshed in lockstep).
  const lastModified =
    [llm.updated, stt.updated, tts.updated].sort().at(-1) ?? new Date().toISOString().slice(0, 10);

  const dispatchRoutes: MetadataRoute.Sitemap = [
    {
      url: absoluteUrl('/'),
      lastModified,
      changeFrequency: 'weekly',
      priority: 1.0,
    },
    {
      url: absoluteUrl('/costs'),
      lastModified,
      changeFrequency: 'weekly',
      priority: 0.9,
    },
    {
      url: absoluteUrl('/costs.md'),
      lastModified,
      changeFrequency: 'weekly',
      priority: 0.7,
    },
  ];

  const proseRoutes: MetadataRoute.Sitemap = source.getPages().map((page) => ({
    url: absoluteUrl(page.url),
    lastModified,
    changeFrequency: 'monthly',
    priority: 0.6,
  }));

  return [...dispatchRoutes, ...proseRoutes];
}
