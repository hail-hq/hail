import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { source } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.pageTree}
      // Absolute, not basePath-relative: these point back at the marketing app
      // on the apex, which is a different Next zone.
      nav={{ title: "Hail docs", url: "https://hail.so" }}
      links={[
        { text: "Pricing", url: "https://hail.so/pricing" },
        { text: "GitHub", url: "https://github.com/hail-hq/hail" },
      ]}
    >
      {children}
    </DocsLayout>
  );
}
