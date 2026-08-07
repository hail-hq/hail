import { openapi } from "@/lib/openapi";

// Server-side relay for the API playground. The browser can't call
// api.hail.so from a hail.so page (CORS), so the playground posts here and we
// forward. `allowedOrigins` is the SSRF guard: this route forwards to the Hail
// API and nothing else — any other target (including redirects) gets a 400.
// Auth is pass-through; the user's API key travels in the proxied request's
// Authorization header and is never stored here.
const proxy = openapi.createProxy({
  allowedOrigins: ["https://api.hail.so"],
});

export const { GET, POST, PUT, DELETE, PATCH, HEAD } = proxy;
