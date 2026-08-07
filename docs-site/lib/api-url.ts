/**
 * Origin of the Hail API the docs playground proxies to.
 *
 * HAIL_API_URL is the stack-wide convention (docker-compose sets it per
 * service, .env.example documents it), so a self-hosted docs-site pointed at
 * its own API works with the var it already has. Unset — the Vercel deploy —
 * means Hail Cloud. `new URL(...).origin` normalizes paths and trailing
 * slashes away; only the origin matters for the proxy allowlist.
 */
export const HAIL_API_ORIGIN = new URL(
  process.env.HAIL_API_URL || "https://api.hail.so",
).origin;
