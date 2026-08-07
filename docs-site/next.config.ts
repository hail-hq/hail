import { createMDX } from "fumadocs-mdx/next";
import type { NextConfig } from "next";

const withMDX = createMDX();

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Served at hail.so/docs via a cross-zone rewrite from the marketing app.
  // basePath keeps this app's /_next/* assets namespaced so they can't collide
  // with the apex app's, and makes the standalone deployment browsable too.
  basePath: "/docs",
  async redirects() {
    // Aug 2026 IA reorg: cloud docs moved to the top level, provisioning docs
    // under self-host/. Sources are basePath-relative (Next prefixes /docs).
    // 308 — the old URLs were live and indexed; the move is permanent.
    return [
      { source: "/setup", destination: "/self-host", permanent: true },
      { source: "/setup/mcp", destination: "/mcp", permanent: true },
      { source: "/setup/webhooks", destination: "/webhooks", permanent: true },
      { source: "/setup/twilio", destination: "/self-host/twilio", permanent: true },
      { source: "/setup/livekit-cloud", destination: "/self-host/livekit-cloud", permanent: true },
      { source: "/setup/aws-ses", destination: "/self-host/aws-ses", permanent: true },
      { source: "/operations", destination: "/self-host/operations", permanent: true },
    ];
  },
};

export default withMDX(nextConfig);
