import { createMDX } from "fumadocs-mdx/next";
import type { NextConfig } from "next";

const withMDX = createMDX();

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Served at hail.so/docs via a cross-zone rewrite from the marketing app.
  // basePath keeps this app's /_next/* assets namespaced so they can't collide
  // with the apex app's, and makes the standalone deployment browsable too.
  basePath: "/docs",
};

export default withMDX(nextConfig);
