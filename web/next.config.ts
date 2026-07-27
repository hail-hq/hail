import type { NextConfig } from "next";

// The raw deployment host was serving the crawl trap directly, bypassing the
// hail.so rewrite. Redirecting it also removes the duplicate-content problem.
const VERCEL_HOST = "hail-costs.vercel.app";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  typedRoutes: true,
  basePath: "/costs",
  async redirects() {
    return [
      {
        source: "/costs/:path*",
        has: [{ type: "host", value: VERCEL_HOST }],
        destination: "https://hail.so/costs/:path*",
        permanent: true,
        basePath: false,
      },
    ];
  },
};

export default nextConfig;
