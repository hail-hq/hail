import { RootProvider } from "fumadocs-ui/provider/next";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import { DOCS_SITE_COPY } from "@/lib/site-copy";
import "./global.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://hail.so"),
  title: { default: DOCS_SITE_COPY.title, template: "%s / Hail docs" },
  description: DOCS_SITE_COPY.description,
  applicationName: DOCS_SITE_COPY.title,
  alternates: { canonical: "/docs" },
  openGraph: {
    type: "website",
    url: "/docs",
    siteName: "Hail",
    title: DOCS_SITE_COPY.title,
    description: DOCS_SITE_COPY.description,
    images: [
      {
        url: "/docs/opengraph-image",
        width: 1200,
        height: 630,
        alt: "Hail docs. Phone, SMS, and email for AI agents.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: DOCS_SITE_COPY.title,
    description: DOCS_SITE_COPY.description,
    images: ["/docs/opengraph-image"],
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="hail-docs flex min-h-screen flex-col">
        <RootProvider>{children}</RootProvider>
      </body>
    </html>
  );
}
