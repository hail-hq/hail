import type { MetadataRoute } from 'next';
import { llm, stt, tts } from '@/lib/costs';
import { absoluteUrl } from '@/lib/url';

export default function sitemap(): MetadataRoute.Sitemap {
  // `updated` on each dataset is the same ISO date (refreshed in lockstep).
  // Use whichever is most recent across the three as the lastModified for the
  // costs pages — when prose docs land they can have their own per-file dates.
  const lastModified =
    [llm.updated, stt.updated, tts.updated].sort().at(-1) ?? new Date().toISOString().slice(0, 10);

  return [
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
    {
      url: absoluteUrl('/costs/compare'),
      lastModified,
      changeFrequency: 'monthly',
      priority: 0.5,
    },
  ];
}
