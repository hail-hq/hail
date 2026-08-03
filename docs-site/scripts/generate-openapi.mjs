import { rm } from "node:fs/promises";
import { generateFiles } from "fumadocs-openapi";
import { openapi } from "../lib/openapi.ts";

// Regenerate the API-reference MDX from the OpenAPI spec into content/api.
// Grouped by tag so the sidebar mirrors the spec's own sections.
await rm("content/api", { recursive: true, force: true });
await generateFiles({
  input: openapi,
  output: "content/api",
  per: "operation",

});
console.log("generated API reference into content/api");
