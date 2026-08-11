import { ImageResponse } from 'next/og';
import { OG_SIZE, OgArt } from '@/lib/og-art';

export const alt = 'Hail — AI model pricing database';
export const size = OG_SIZE;
export const contentType = 'image/png';

export default function Image() {
  return new ImageResponse(<OgArt />, size);
}
