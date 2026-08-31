import { DocsBody, DocsPage } from "fumadocs-ui/page";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ComponentProps } from "react";
import { OpenAPIPage } from "@/components/openapi-page";
import { openapi } from "@/lib/openapi";
import { getMDXComponents } from "@/mdx-components";
import { apiSource } from "@/lib/source";

// The /docs/api root has no generated page, so render an index that lists every
// operation. Deeper slugs render the generated OpenAPI page for one endpoint.
function ApiIndex() {
  const pages = apiSource
    .getPages()
    .slice()
    .sort((a, b) => a.data.title.localeCompare(b.data.title));
  return (
    <DocsPage>
      <DocsBody>
        <h1>API Reference</h1>
        <p>
          Hail exposes a REST API at <code>https://api.hail.so</code>. Every
          endpoint below is generated from <code>openapi/openapi.yaml</code>,
          the canonical contract that CI verifies against the live service, so
          this reference never drifts from the code.
        </p>
        <p>
          Machine-readable spec:{" "}
          <a href="/docs/api/openapi.yaml">openapi.yaml</a> ·{" "}
          <a href="/docs/api/openapi.json">openapi.json</a>
        </p>
        <p>
          Every page below (and every guide) is also available as plain markdown
          — append <code>.md</code> to its URL, or fetch{" "}
          <a href="/docs/llms.txt">llms.txt</a> (index) or{" "}
          <a href="/docs/llms-full.txt">llms-full.txt</a> (everything,
          concatenated).
        </p>
        <ul>
          {pages.map((page) => (
            <li key={page.url}>
              <Link href={page.url}>{page.data.title}</Link>
            </li>
          ))}
        </ul>
      </DocsBody>
    </DocsPage>
  );
}

export default async function Page(props: {
  params: Promise<{ slug?: string[] }>;
}) {
  const { slug } = await props.params;
  if (!slug || slug.length === 0) return <ApiIndex />;

  const page = apiSource.getPage(slug);
  if (!page) notFound();

  // The generated MDX renders <OpenAPIPage document operations/>, but the client
  // component needs the bundled schema too. Preload it on the server and inject.
  const { preloaded } = await openapi.preloadOpenAPIPage(page);
  const PreloadedOpenAPIPage = (p: ComponentProps<typeof OpenAPIPage>) => (
    <OpenAPIPage {...p} preloaded={preloaded} />
  );

  const MDX = page.data.body;
  return (
    <DocsPage toc={page.data.toc} full={page.data.full}>
      <DocsBody>
        <MDX
          components={getMDXComponents({ OpenAPIPage: PreloadedOpenAPIPage })}
        />
      </DocsBody>
    </DocsPage>
  );
}

export function generateStaticParams() {
  return apiSource.generateParams();
}

export async function generateMetadata(props: {
  params: Promise<{ slug?: string[] }>;
}): Promise<Metadata> {
  const { slug } = await props.params;
  if (!slug || slug.length === 0) {
    return {
      title: "API Reference",
      description: "REST API reference for Hail.",
    };
  }
  const page = apiSource.getPage(slug);
  if (!page) notFound();
  return { title: page.data.title, description: page.data.description };
}
