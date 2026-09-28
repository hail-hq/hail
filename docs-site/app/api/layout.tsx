import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { apiSource } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={apiSource.pageTree}
      nav={{ title: "api reference", url: "/api" }}
      links={[{ text: "guides", url: "/" }]}
    >
      {children}
    </DocsLayout>
  );
}
