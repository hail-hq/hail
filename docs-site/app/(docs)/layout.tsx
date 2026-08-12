import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { DocsBrand, DocsCta } from "@/components/docs-brand";
import { source } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={source.pageTree}
      // Absolute, not basePath-relative: these point back at the marketing app
      // on the apex, which is a different Next zone.
      nav={{ title: <DocsBrand />, url: "https://hail.so" }}
      links={[
        { text: "api reference", url: "/api" },
        { text: "pricing", url: "https://hail.so/pricing" },
        { text: "github", url: "https://github.com/hail-hq/hail" },
        {
          type: "button",
          text: <DocsCta>get started</DocsCta>,
          url: "https://hail.so/signup",
          secondary: false,
        },
      ]}
    >
      {children}
    </DocsLayout>
  );
}
