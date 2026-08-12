import { DocsLayout } from "fumadocs-ui/layouts/docs";
import type { ReactNode } from "react";
import { DocsBrand, DocsCta } from "@/components/docs-brand";
import { apiSource } from "@/lib/source";

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <DocsLayout
      tree={apiSource.pageTree}
      nav={{ title: <DocsBrand />, url: "/" }}
      links={[
        { text: "guides", url: "/" },
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
