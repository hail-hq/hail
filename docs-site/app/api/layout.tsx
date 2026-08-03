import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { apiSource } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={apiSource.pageTree}
      nav={{ title: "Hail API", url: "/" }}
      links={[
        { text: "Docs", url: "/" },
        { text: "GitHub", url: "https://github.com/hail-hq/hail" },
      ]}
    >
      {children}
    </DocsLayout>
  );
}
