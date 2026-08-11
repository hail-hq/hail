import { ImageResponse } from 'next/og';
import { OG_SIZE, OgArt } from '@/lib/og-art';

export const alt = 'Hail — AI model pricing database';
export const size = OG_SIZE;
export const contentType = 'image/png';
// Depends only on the constants in lib/og-art.tsx — bake the PNG once at
// build instead of running Satori + resvg per crawler hit.
export const dynamic = 'force-static';

export default function Image() {
  return new ImageResponse(<OgArt />, size);
}
