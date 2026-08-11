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

export const metadata = {
  metadataBase: siteUrl,
  title: 'Hail · model costs',
  description:
    'Public, validated pricing and capability data for AI model providers — LLMs, speech-to-text, and text-to-speech.',
  icons: {
    icon: '/costs/icon',
    apple: '/costs/apple-icon',
  },
  openGraph: {
    siteName: 'Hail',
    type: 'website',
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
    images: [
      {
        url: '/costs/twitter-image',
        width: 1200,
        height: 630,
        alt: 'Hail — AI model pricing database',
      },
    ],
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
