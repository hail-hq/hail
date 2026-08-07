import { HAIL_API_ORIGIN } from "@/lib/api-url";
import { openapi } from "@/lib/openapi";

// Server-side relay for the API playground. The browser can't call the API
// origin from a hail.so page (CORS), so the playground posts here and we
// forward. `allowedOrigins` is the SSRF guard: this route forwards to the Hail
// API (HAIL_API_URL, default api.hail.so) and nothing else — any other target
// (including redirects) gets a 400. Auth is pass-through; the user's API key
// travels in the proxied request's Authorization header and is never stored.
const proxy = openapi.createProxy({
  allowedOrigins: [HAIL_API_ORIGIN],
});

export const { GET, POST, PUT, DELETE, PATCH, HEAD } = proxy;
