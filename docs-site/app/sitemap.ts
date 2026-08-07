import type { MetadataRoute } from "next";
import { apiSource, source } from "@/lib/source";

// Served at hail.so/docs/sitemap.xml (basePath applies to metadata routes).
// This app owns the whole /docs surface — prose pages AND the generated API
// reference — so the apex sitemap must not also list /docs URLs; it references
// this file from robots.txt instead (same split as the costs app).
const ORIGIN = "https://hail.so";

export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();

  const prose: MetadataRoute.Sitemap = source.getPages().map((page) => ({
    // page.url is loader-relative ("/architecture", "/" for the index); the
    // public URL adds the /docs basePath.
    url: `${ORIGIN}/docs${page.url === "/" ? "" : page.url}`,
    lastModified: now,
    changeFrequency: "weekly",
    priority: page.url === "/" ? 0.9 : 0.7,
  }));

  const api: MetadataRoute.Sitemap = [
    {
      url: `${ORIGIN}/docs/api`,
      lastModified: now,
      changeFrequency: "weekly",
      priority: 0.8,
    },
    ...apiSource.getPages().map((page) => ({
      url: `${ORIGIN}/docs${page.url}`,
      lastModified: now,
      changeFrequency: "weekly" as const,
      priority: 0.5,
    })),
  ];

  return [...prose, ...api];
}
