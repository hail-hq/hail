import { notFound } from "next/navigation";
import { getDocsLLMText } from "@/lib/get-llm-text";
import {
  getOperationMarkdown,
  renderApiIndexMarkdown,
} from "@/lib/openapi-markdown";
import { apiSource, source } from "@/lib/source";

// Backs the `/:path*.md` rewrite in next.config.ts — every docs page and every
// generated API-reference page is available as plain markdown by appending
// `.md` to its URL. `revalidate: false` matches the rest of the docs-site:
// content only changes on a new deploy.
export const revalidate = false;

const MARKDOWN_HEADERS = { "content-type": "text/markdown; charset=utf-8" };

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ slug?: string[] }> },
) {
  const { slug = [] } = await params;

  if (slug[0] === "api") {
    const apiSlug = slug.slice(1);
    if (apiSlug.length === 0) {
      return new Response(renderApiIndexMarkdown(apiSource.getPages()), {
        headers: MARKDOWN_HEADERS,
      });
    }
    const page = apiSource.getPage(apiSlug);
    if (!page) notFound();
    const markdown = await getOperationMarkdown(page);
    if (!markdown) notFound();
    return new Response(markdown, { headers: MARKDOWN_HEADERS });
  }

  const page = source.getPage(slug);
  if (!page) notFound();
  return new Response(await getDocsLLMText(page), {
    headers: MARKDOWN_HEADERS,
  });
}

export function generateStaticParams() {
  const docsParams = source.generateParams();
  const apiParams = apiSource
    .generateParams()
    .map(({ slug = [] }) => ({ slug: ["api", ...slug] }));
  return [...docsParams, { slug: ["api"] }, ...apiParams];
}
