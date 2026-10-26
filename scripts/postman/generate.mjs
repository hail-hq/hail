import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";
import { quickstartItems, quickstartVariables } from "./quickstart.mjs";
import { httpMethods, referenceItems } from "./reference.mjs";

export const root = new URL("../../", import.meta.url);
const schema =
  "https://schema.getpostman.com/json/collection/v2.1.0/collection.json";
const publicPath = (path) =>
  path === "/healthz" ||
  (path.startsWith("/v1/") &&
    !/\/(internal|admin|superadmin)(\/|$)/.test(path));

function stableId(key) {
  const hex = createHash("sha256").update(key).digest("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-4${hex.slice(13, 16)}-a${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
}

export function walk(items) {
  return items.flatMap((item) => (item.item ? walk(item.item) : [item]));
}

// The same route can appear in Start here and in the reference, so request
// IDs include the folder path.
function normalize(items, parents = []) {
  for (const item of items) {
    const path = [...parents, item.name];
    const url = item.request?.url?.raw ?? item.request?.url;
    item.id = stableId(
      item.request
        ? `${parents.join("/")}:${item.request.method}:${url.split("?")[0]}`
        : `folder:${path.join("/")}`,
    );
    if (item.item) normalize(item.item, path);
    for (const [index, response] of (item.response ?? []).entries())
      response.id = stableId(`${item.id}:response:${response.code}:${index}`);
  }
}

export const collectionName = "Hail API";

function finish(collection, overview, variables) {
  collection.info = { name: collectionName, schema, description: overview };
  collection.auth = {
    type: "bearer",
    bearer: [{ key: "token", value: "{{api_key}}", type: "string" }],
  };
  collection.variable = [
    { key: "baseUrl", value: "https://api.hail.so", type: "string" },
    // Empty: each user pastes their own key. Postman keeps it local by default.
    { key: "api_key", value: "", type: "string" },
    ...variables,
  ];
  normalize(collection.item);
  return collection;
}

export async function generate() {
  const spec = parse(
    await readFile(new URL("openapi/openapi.yaml", root), "utf8"),
  );
  const overview = await readFile(new URL("postman/README.md", root), "utf8");
  spec.paths = Object.fromEntries(
    Object.entries(spec.paths).filter(([path]) => publicPath(path)),
  );
  const start = {
    name: "Start here",
    description:
      "Run these steps in order. Each step saves the IDs the next steps use. Requests that spend credit are skipped until enable_paid_requests is true.",
    item: quickstartItems(),
  };
  const reference = referenceItems(spec);
  const expected = Object.values(spec.paths).reduce(
    (sum, path) =>
      sum + Object.keys(path).filter((m) => httpMethods.has(m)).length,
    0,
  );
  if (walk(reference).length !== expected)
    throw new Error("Conversion lost or duplicated public operations");
  const collection = finish(
    { item: [start, ...reference] },
    overview,
    quickstartVariables,
  );
  const output = new URL("postman/", root);
  await mkdir(output, { recursive: true });
  await writeFile(
    new URL("hail-api.postman_collection.json", output),
    `${JSON.stringify(collection, null, 2)}\n`,
  );
  return { collection, start, spec };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { collection, start } = await generate();
  console.log(
    `Generated ${walk(collection.item).length} requests, ${walk(start.item).length} in Start here.`,
  );
}
