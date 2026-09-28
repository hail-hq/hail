import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { source } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.pageTree}
      // Keep documentation navigation local; the site header owns cross-zone links.
      nav={{ title: "documentation", url: "/" }}
      links={[{ text: "api reference", url: "/api" }]}
    >
      {children}
    </DocsLayout>
  );
}
