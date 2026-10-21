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

function normalize(items, namespace, parents = []) {
  for (const item of items) {
    const path = [...parents, item.name];
    const url = item.request?.url?.raw ?? item.request?.url;
    item.id = stableId(
      item.request
        ? `${namespace}:${item.request.method}:${url.split("?")[0]}`
        : `${namespace}:folder:${path.join("/")}`,
    );
    if (item.item) normalize(item.item, namespace, path);
    for (const [index, response] of (item.response ?? []).entries())
      response.id = stableId(`${item.id}:response:${response.code}:${index}`);
  }
}

function finish(collection, name, overview, variables = []) {
  collection.info = { name, schema, description: overview };
  collection.auth = {
    type: "bearer",
    bearer: [{ key: "token", value: "{{api_key}}", type: "string" }],
  };
  collection.variable = [
    { key: "baseUrl", value: "https://api.hail.so", type: "string" },
    { key: "api_key", value: "{{vault:hail-api-key}}", type: "string" },
    ...variables,
  ];
  normalize(collection.item, name);
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
  const reference = finish(
    { item: referenceItems(spec) },
    "Hail API Reference",
    overview,
  );
  const expected = Object.values(spec.paths).reduce(
    (sum, path) =>
      sum + Object.keys(path).filter((m) => httpMethods.has(m)).length,
    0,
  );
  if (walk(reference.item).length !== expected)
    throw new Error("Conversion lost or duplicated public operations");
  const quickstart = finish(
    { item: quickstartItems() },
    "Hail API: Start here",
    overview,
    quickstartVariables,
  );
  const output = new URL("postman/", root);
  await mkdir(output, { recursive: true });
  for (const [name, collection] of [
    ["hail-api-reference", reference],
    ["hail-start-here", quickstart],
  ]) {
    await writeFile(
      new URL(`${name}.postman_collection.json`, output),
      `${JSON.stringify(collection, null, 2)}\n`,
    );
  }
  return { reference, quickstart, spec };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { reference, quickstart } = await generate();
  console.log(
    `Generated ${walk(reference.item).length} reference requests and ${walk(quickstart.item).length} quickstart requests.`,
  );
}
