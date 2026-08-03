import type { NextConfig } from "next";

// No canonical-host redirect here. hail-website rewrites /costs/* to this
// deployment, so a proxied request presents the same Host as a direct hit on
// the raw *.vercel.app domain — a host-based redirect cannot tell them apart
// and loops against the rewrite. See PR #54 / the 2026-07-27 incident.
//
// No PostHog /ingest reverse-proxy rewrite either: NEXT_PUBLIC_POSTHOG_HOST is
// t.hail.so, already a PostHog custom domain, so the client posts there
// directly. A rewrite here would also have to carry the /costs basePath, which
// the generated default did not.

const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  basePath: "/costs",
};

export default nextConfig;
