import { createSearchAPI } from "fumadocs-core/search/server";
import { apiSource, source } from "@/lib/source";

// Search the same public guide and generated API sources that own navigation.
// Results remain basePath-relative so Fumadocs' Next links add /docs once.
export const { GET } = createSearchAPI("advanced", {
  indexes: [...source.getPages(), ...apiSource.getPages()].map((page) => ({
    id: page.url,
    url: page.url,
    title: page.data.title,
    description: page.data.description,
    structuredData: page.data.structuredData,
  })),
});
