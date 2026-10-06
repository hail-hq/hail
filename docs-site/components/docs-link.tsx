import { createRelativeLink } from "fumadocs-ui/mdx";
import type { ComponentProps } from "react";
import { repositoryLink } from "@/lib/repository-link";
import { source } from "@/lib/source";

type DocsPage = NonNullable<ReturnType<typeof source.getPage>>;

export function createDocsLink(page: DocsPage) {
  const RelativeLink = createRelativeLink(source, page);
  return function DocsLink({ href, ...props }: ComponentProps<"a">) {
    return <RelativeLink {...props} href={repositoryLink(href, page.path)} />;
  };
}
