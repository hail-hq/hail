import { loader } from "fumadocs-core/source";
import { apiDocs, docs } from "@/.source/server";
import { slugSegments } from "@/lib/doc-slug.mjs";

/**
 * `baseUrl` is "/", not "/docs" — Next's `basePath: "/docs"` already prefixes
 * every route and link, so setting it here too would emit /docs/docs/*.
 *
 * The slug rule (README = folder index) lives in doc-slug.mjs so scripts/urls.mjs
 * can share it verbatim — the drift guard is only meaningful if both agree.
 */
export const source = loader({
  baseUrl: "/",
  source: docs.toFumadocsSource(),
  slugs: (file) => slugSegments(file.path),
});

/**
 * The generated API reference lives at /docs/api/*. baseUrl "/api" → with the
 * app's /docs basePath the pages resolve to /docs/api/<operation>.
 */
export const apiSource = loader({
  baseUrl: "/api",
  source: apiDocs.toFumadocsSource(),
});
