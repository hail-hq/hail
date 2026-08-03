import { RootProvider } from "fumadocs-ui/provider/next";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./global.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://hail.so"),
  title: { default: "Hail docs", template: "%s — Hail docs" },
  description:
    "Documentation for Hail — the universal communication platform for AI agents: phone calls, SMS, and email over one MCP endpoint.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="flex min-h-screen flex-col">
        <RootProvider>{children}</RootProvider>
      </body>
    </html>
  );
}
