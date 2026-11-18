import type { ReactNode } from "react";
import localFont from "next/font/local";
import { siteUrl } from "@/lib/url";
import {
  COSTS_HERO_COPY,
  COSTS_SITE_DESCRIPTION,
  COSTS_SITE_TITLE,
} from "@/lib/site-copy";
import "./global.css";
import "@/components/hail-design/tokens.css";
import "@/components/hail-design/typography.css";
import "./public.css";

// Every font loads from npm package files, so the build downloads nothing. A Google Fonts
// download that failed broke a hail.so production build.
const fontDisplay = localFont({
  src: "../node_modules/@fontsource-variable/bricolage-grotesque/files/bricolage-grotesque-latin-wght-normal.woff2",
  weight: "200 800",
  variable: "--font-bricolage",
  display: "swap",
});

// The portable tokens map these local files to --font-ui and --font-code.
const fontUi = localFont({
  src: "../node_modules/geist/dist/fonts/geist-sans/Geist-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist-sans",
  display: "swap",
});

const fontCode = localFont({
  src: "../node_modules/geist/dist/fonts/geist-mono/GeistMono-Variable.woff2",
  weight: "100 900",
  variable: "--font-geist-mono",
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

// Icons/OG match hail.so's own app/layout.tsx (same bird icon, same palette,
// same og:image template) so /costs reads as a page of the site rather than
// a neighbouring property. The shared public tokens own its palette. Paths need
// the explicit /costs prefix: Next's basePath config isn't applied to the
// absolute URLs it derives for file-convention icons/og-image, so without
// this they'd resolve to hail.so/icon instead of hail.so/costs/icon.
export const metadata = {
  metadataBase: siteUrl,
  title: COSTS_SITE_TITLE,
  description: COSTS_SITE_DESCRIPTION,
  icons: {
    icon: [
      {
        url: "/costs/assets/favicon-32.png",
        sizes: "32x32",
        type: "image/png",
      },
      {
        url: "/costs/assets/favicon-16.png",
        sizes: "16x16",
        type: "image/png",
      },
    ],
    apple: [
      {
        url: "/costs/assets/apple-touch-180.png",
        sizes: "180x180",
        type: "image/png",
      },
    ],
  },
  openGraph: {
    type: "website",
    siteName: "Hail",
    title: COSTS_SITE_TITLE,
    description: COSTS_SITE_DESCRIPTION,
    images: [
      {
        url: "/costs/opengraph-image",
        width: 1200,
        height: 630,
        alt: COSTS_HERO_COPY.heading,
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    site: "@hail_hq",
    creator: "@hail_hq",
    title: COSTS_SITE_TITLE,
    description: COSTS_SITE_DESCRIPTION,
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${fontMono.variable} ${fontDisplay.variable} ${fontUi.variable} ${fontCode.variable}`}
    >
      <body>
        <script
          dangerouslySetInnerHTML={{
            __html: `try{var t=localStorage.getItem('hail-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch{}`,
          }}
        />
        {children}
      </body>
    </html>
  );
}
