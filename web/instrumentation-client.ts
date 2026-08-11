import posthog from "posthog-js";

const key = process.env.NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN;

if (!key) {
  if (process.env.NODE_ENV === "development") {
    throw new Error(
      "NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN variable required by PostHog is missing or un-configured, " +
        "this causes events to be silently missed. This error stops appearing once NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN is configured",
    );
  }
} else {
  posthog.init(key, {
    // t.hail.so is a PostHog custom domain, so events go straight there — no
    // reverse-proxy rewrite needed. Matches hail-website, which uses the same
    // project so /costs traffic joins the same funnel rather than splitting off.
    api_host: process.env.NEXT_PUBLIC_POSTHOG_HOST!,
    ui_host: "https://eu.posthog.com",
    defaults: "2026-01-30",
    capture_exceptions: true,
    debug: process.env.NODE_ENV === "development",
  });
}
