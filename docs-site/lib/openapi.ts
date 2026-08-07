import { createOpenAPI } from "fumadocs-openapi/server";

// The API spec is the repo's canonical contract, CI-verified against the live
// FastAPI app (see ../.github/workflows/openapi-check.yml). Point the reference
// generator straight at it so the docs never drift from the code.
export const openapi = createOpenAPI({
  input: ["../openapi/openapi.yaml"],
  // The playground's "Send" button fires from the browser, where a direct call
  // to api.hail.so is blocked by CORS. Requests go to this same-origin route
  // (app/api/proxy) instead, which forwards them server-side. Path includes the
  // /docs basePath — the client fetches it as-is.
  proxyUrl: "/docs/api/proxy",
});
