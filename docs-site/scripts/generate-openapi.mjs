import { cp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { load as parseYaml } from "js-yaml";
import { generateFiles } from "fumadocs-openapi";
import { openapi } from "../lib/openapi.ts";

// Regenerate the API-reference MDX from the OpenAPI spec into content/api.
await rm("content/api", { recursive: true, force: true });
await generateFiles({
  input: openapi,
  output: "content/api",
  per: "operation",
});
console.log("generated API reference into content/api");

// Publish the spec itself as machine-readable static files. public/ is served
// under the /docs basePath, so these land at /docs/api/openapi.{yaml,json} —
// exact static matches win over the /api/[[...slug]] route. Regenerated before
// every build (both dirs are gitignored); openapi/openapi.yaml stays the only
// committed copy.
const SPEC = "../openapi/openapi.yaml";
await rm("public/api", { recursive: true, force: true });
await mkdir("public/api", { recursive: true });
await cp(SPEC, "public/api/openapi.yaml");
const doc = parseYaml(await readFile(SPEC, "utf8"));
await writeFile("public/api/openapi.json", JSON.stringify(doc, null, 2) + "\n");
console.log("published spec to public/api/openapi.{yaml,json}");
