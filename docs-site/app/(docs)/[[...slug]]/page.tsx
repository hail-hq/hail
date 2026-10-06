import { createDocsLink } from "@/components/docs-link";
import { DocsBody, DocsPage } from "fumadocs-ui/page";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getMDXComponents } from "@/mdx-components";
import { source } from "@/lib/source";
import { docsPath, pageMetadata } from "../../../../shared/site-metadata";

export default async function Page(props: {
  params: Promise<{ slug?: string[] }>;
}) {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();

  const MDX = page.data.body;
  return (
    <DocsPage toc={page.data.toc} full={page.data.full}>
      {/* No DocsTitle here on purpose: every file in docs/public/ opens with
          its own `# H1`, which also has to render correctly on GitHub. Adding
          the title component would print the heading twice. */}
      <DocsBody>
        {/* Preserve relative Markdown links on GitHub. Resolve published guides
            to docs routes and repository source files to GitHub in the renderer. */}
        <MDX components={getMDXComponents({ a: createDocsLink(page) })} />
      </DocsBody>
    </DocsPage>
  );
}

export function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata(props: {
  params: Promise<{ slug?: string[] }>;
}): Promise<Metadata> {
  const { slug } = await props.params;
  const page = source.getPage(slug);
  if (!page) notFound();
  return pageMetadata({
    title: `${page.data.title} | hail.so`,
    description:
      page.url === "/"
        ? "Read Hail’s guides for AI phone calls, SMS, and email. Connect through MCP, use the API or CLI, and learn how to host Hail."
        : (page.data.description ?? `Read the Hail guide: ${page.data.title}.`),
    path: docsPath(page.url),
    image: "/docs/opengraph-image",
  });
}
