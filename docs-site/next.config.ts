import { createMDX } from "fumadocs-mdx/next";
import type { NextConfig } from "next";

const withMDX = createMDX();

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Served at hail.so/docs via a cross-zone rewrite from the marketing app.
  // basePath keeps this app's /_next/* assets namespaced so they can't collide
  // with the apex app's, and makes the standalone deployment browsable too.
  basePath: "/docs",
  async rewrites() {
    // Every doc page and every generated API-reference page is available as
    // plain markdown by appending `.md` — e.g. /docs/architecture.md,
    // /docs/api/create_email_emails_post.md. Handled by app/llms.mdx/[[...slug]],
    // which knows both sources; this just maps the friendly suffix onto it.
    return [{ source: "/:path*.md", destination: "/llms.mdx/:path*" }];
  },
  async redirects() {
    // Aug 2026 IA reorg: cloud docs moved to the top level, provisioning docs
    // under self-host/. Sources are basePath-relative (Next prefixes /docs).
    // 308 — the old URLs were live and indexed; the move is permanent.
    return [
      { source: "/setup", destination: "/self-host", permanent: true },
      { source: "/setup/mcp", destination: "/mcp", permanent: true },
      { source: "/setup/webhooks", destination: "/webhooks", permanent: true },
      {
        source: "/setup/twilio",
        destination: "/self-host/twilio",
        permanent: true,
      },
      {
        source: "/setup/livekit-cloud",
        destination: "/self-host/livekit-cloud",
        permanent: true,
      },
      {
        source: "/setup/aws-ses",
        destination: "/self-host/aws-ses",
        permanent: true,
      },
      {
        source: "/operations",
        destination: "/self-host/operations",
        permanent: true,
      },
    ];
  },
};

export default withMDX(nextConfig);
