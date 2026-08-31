import { apiSource, source } from "@/lib/source";

// Absolute origin, matching the precedent in app/sitemap.ts: this file is
// meant to be fetched and read outside the site (an agent's context window),
// so its links need to resolve on their own, not just relative to a browser
// tab. Self-hosted deployments don't need this file to point at themselves.
const ORIGIN = "https://hail.so/docs";

export const revalidate = false;

export function GET() {
  const guides = source
    .getPages()
    .slice()
    .sort((a, b) => a.url.localeCompare(b.url))
    .map((page) => {
      const url = `${ORIGIN}${page.url === "/" ? "" : page.url}.md`;
      const description = page.data.description
        ? `: ${page.data.description}`
        : "";
      return `- [${page.data.title}](${url})${description}`;
    });

  const operations = apiSource
    .getPages()
    .slice()
    .sort((a, b) => a.url.localeCompare(b.url))
    .map((page) => `- [${page.data.title}](${ORIGIN}${page.url}.md)`);

  const body = [
    "# Hail",
    "Universal communication platform for AI agents — outbound calls, SMS, and email over one REST API, CLI, and MCP server.",
    "## Guides",
    guides.join("\n"),
    "## API reference",
    `Full list: ${ORIGIN}/api.md`,
    operations.join("\n"),
  ].join("\n\n");

  return new Response(`${body}\n`, {
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
}
