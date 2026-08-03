import { createOpenAPI } from "fumadocs-openapi/server";

// The API spec is the repo's canonical contract, CI-verified against the live
// FastAPI app (see ../.github/workflows/openapi-check.yml). Point the reference
// generator straight at it so the docs never drift from the code.
export const openapi = createOpenAPI({
  input: ["../openapi/openapi.yaml"],
});
