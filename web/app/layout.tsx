import type { ReactNode } from 'react';
import { Space_Grotesk, JetBrains_Mono, Instrument_Serif } from 'next/font/google';
import { siteUrl } from '@/lib/url';
import './global.css';

const fontSans = Space_Grotesk({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-sans',
  display: 'swap',
});

const fontMono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '700'],
  variable: '--font-mono',
  display: 'swap',
});

const fontSerif = Instrument_Serif({
  subsets: ['latin'],
  weight: '400',
  style: 'italic',
  variable: '--font-serif',
  display: 'swap',
});

// Icons/OG match hail.so's own app/layout.tsx (same monogram, same palette,
// same og:image template) so /costs reads as a page of the site rather than
// a neighbouring property — see the palette note in global.css. Paths need
// the explicit /costs prefix: Next's basePath config isn't applied to the
// absolute URLs it derives for file-convention icons/og-image, so without
// this they'd resolve to hail.so/icon instead of hail.so/costs/icon.
export const metadata = {
  metadataBase: siteUrl,
  title: 'Hail · model costs',
  description:
    'Public, validated pricing and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech.',
  icons: {
    icon: [
      { url: '/costs/assets/favicon-32.png', sizes: '32x32', type: 'image/png' },
      { url: '/costs/assets/favicon-16.png', sizes: '16x16', type: 'image/png' },
    ],
    apple: [{ url: '/costs/assets/apple-touch-180.png', sizes: '180x180', type: 'image/png' }],
  },
  openGraph: {
    type: 'website',
    siteName: 'Hail',
    images: [
      {
        url: '/costs/opengraph-image',
        width: 1200,
        height: 630,
        alt: 'Hail — AI model pricing database',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    site: '@hail_hq',
    creator: '@hail_hq',
  },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      suppressHydrationWarning
      className={`${fontSans.variable} ${fontMono.variable} ${fontSerif.variable}`}
    >
      <body>{children}</body>
    </html>
  );
}
