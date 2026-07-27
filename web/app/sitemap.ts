import type { MetadataRoute } from "next";
import { featuredPairs } from "@/lib/featured";
import { SITE_ORIGIN } from "@/lib/url";

export const dynamic = "force-static";

const abs = (path: string) => new URL(path, SITE_ORIGIN).toString();

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: abs("/costs"), changeFrequency: "weekly", priority: 1 },
    { url: abs("/costs/compare"), changeFrequency: "weekly", priority: 0.8 },
    ...featuredPairs.map((p) => ({
      url: abs(`/costs/compare/${p.slug}`),
      changeFrequency: "weekly" as const,
      priority: 0.6,
    })),
  ];
}
