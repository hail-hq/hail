import type { NextConfig } from 'next';
import { BASE_PATH } from './lib/url';

const nextConfig: NextConfig = {
  basePath: BASE_PATH,
  reactStrictMode: true,
  typedRoutes: true,
  turbopack: {
    rules: {
      '*.{md,mdx}': {
        loaders: ['@mdx-js/loader'],
        as: '*.tsx',
      },
    },
  },
};

export default nextConfig;
