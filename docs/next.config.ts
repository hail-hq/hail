import type { NextConfig } from 'next';
import { createMDX } from 'fumadocs-mdx/next';
import { BASE_PATH } from './lib/url';

const withMDX = createMDX();

const nextConfig: NextConfig = {
  basePath: BASE_PATH,
  reactStrictMode: true,
  typedRoutes: true,
};

export default withMDX(nextConfig);
