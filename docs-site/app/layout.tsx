import { SiteHeader } from "@/components/hail-header/SiteHeader";
import { HeaderTheme } from "@/components/header-theme";
import { SiteFooter } from "@/components/site-footer";
import { RootProvider } from "fumadocs-ui/provider/next";
import type { Metadata } from "next";
import localFont from "next/font/local";
import type { ReactNode } from "react";
import { DOCS_SITE_COPY } from "@/lib/site-copy";
import "./global.css";

// Every font loads from npm package files, so the build downloads nothing. A Google Fonts
// download that failed broke a hail.so production build.
const fontSans = localFont({
  src: "../node_modules/@fontsource-variable/space-grotesk/files/space-grotesk-latin-wght-normal.woff2",
  weight: "300 700",
  variable: "--font-grotesk",
  display: "swap",
});

// The shared hail.so header and footer (components/hail-header, hail-footer) read --font-ui and --font-code.
const fontUi = localFont({
  src: "../node_modules/geist/dist/fonts/geist-sans/Geist-Variable.woff2",
  weight: "100 900",
  variable: "--font-ui",
  display: "swap",
});

const fontCode = localFont({
  src: "../node_modules/geist/dist/fonts/geist-mono/GeistMono-Variable.woff2",
  weight: "100 900",
  variable: "--font-code",
  display: "swap",
  adjustFontFallback: false,
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
});

const fontMono = localFont({
  src: "../node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2",
  weight: "300 700",
  variable: "--font-mono",
  display: "swap",
  adjustFontFallback: false,
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
});

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
    <html
      lang="en"
      suppressHydrationWarning
      className={`${fontMono.variable} ${fontSans.variable} ${fontUi.variable} ${fontCode.variable}`}
    >
      <body className="hail-docs flex min-h-screen flex-col">
        <RootProvider theme={{ storageKey: "hail-theme" }}>
          <SiteHeader active="docs" themeControl={<HeaderTheme />} />
          {children}
          <SiteFooter />
        </RootProvider>
      </body>
    </html>
  );
}
