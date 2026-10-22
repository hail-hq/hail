import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { generate, root, walk } from "./generate.mjs";
import { updateCollection } from "./publish.mjs";
import { resolve } from "./reference.mjs";

const { collection, start, spec } = await generate();

test("Start here requests use current public routes, fields, and query parameters", () => {
  for (const { request } of walk(start.item)) {
    const raw = request.url.raw ?? request.url;
    const [path, query] = raw.replace("{{baseUrl}}", "").split("?");
    const template = path.replace(/\{\{([^}]+)\}\}/g, "{$1}");
    const operation = spec.paths[template]?.[request.method.toLowerCase()];
    assert.ok(operation, `${request.method} ${template} no longer exists`);
    for (const key of new URLSearchParams(query).keys()) {
      assert.ok(
        operation.parameters?.some((p) => p.name === key && p.in === "query"),
        `Unknown query field ${key}`,
      );
    }
    if (!request.body) continue;
    const example = JSON.parse(
      request.body.raw.replaceAll("{{expected_total_cents}}", "100"),
    );
    checkBody(
      example,
      operation.requestBody.content["application/json"].schema,
      `${request.method} ${path}`,
    );
  }
});

// Check required and allowed fields, including nested objects.
function checkBody(value, input, where) {
  const schema = resolve(spec, input);
  const options = schema.anyOf ?? schema.oneOf;
  if (options) {
    const object = options
      .map((s) => resolve(spec, s))
      .find((s) => s.properties);
    if (object) checkBody(value, object, where);
    return;
  }
  if (!schema.properties || typeof value !== "object" || value === null) return;
  for (const key of schema.required ?? [])
    assert.ok(key in value, `${where} needs ${key}`);
  for (const [key, field] of Object.entries(value)) {
    if (schema.additionalProperties === false)
      assert.ok(key in schema.properties, `${where}: unknown field ${key}`);
    if (schema.properties[key])
      checkBody(field, schema.properties[key], `${where}.${key}`);
  }
}

test("export has unique stable IDs and no internal requests or fake auth headers", async () => {
  const repeated = await generate();
  const items = walk(collection.item);
  assert.equal(new Set(items.map((i) => i.id)).size, items.length);
  assert.deepEqual(
    items.map((i) => i.id),
    walk(repeated.collection.item).map((i) => i.id),
  );
  assert.equal(collection.item[0].name, "Start here");
  assert.equal(collection.auth.bearer[0].value, "{{api_key}}");
  assert.equal(
    collection.variable.find((v) => v.key === "api_key").value,
    "",
  );
  for (const { request } of items) {
    const url = request.url.raw ?? request.url;
    assert.doesNotMatch(url, /\/(internal|admin|superadmin)(\/|$)/);
    assert.ok(
      !request.header.some((h) => h.key.toLowerCase() === "authorization"),
    );
  }
  assert.match(collection.info.description, /https:\/\/hail.so\/signup/);
  const source = await readFile(new URL("postman/README.md", root), "utf8");
  assert.equal(collection.info.description, source);
});

// Run a Start here script against a fake Postman sandbox.
function run(name, listen, { response, enabled } = {}) {
  const saved = {};
  let skipped = false;
  const script = walk(start.item)
    .find((i) => i.name === name)
    .event.find((e) => e.listen === listen)
    .script.exec.join("\n");
  new Function("pm", "console", script)(
    {
      variables: { get: () => enabled },
      collectionVariables: { set: (key, value) => (saved[key] = value) },
      execution: {
        skipRequest: () => {
          skipped = true;
        },
      },
      response: response && {
        code: response.code,
        json: () => response.body,
      },
    },
    { warn() {} },
  );
  return { saved, skipped };
}

test("Start here saves IDs for the next steps", () => {
  const offers = [
    {
      quote_id: "q1",
      number_type: "local",
      monthly_cents: 100,
      setup_cents: 0,
    },
    {
      quote_id: "q2",
      number_type: "mobile",
      monthly_cents: 300,
      setup_cents: 50,
    },
  ];
  assert.deepEqual(
    run("Get number quotes", "test", {
      response: { code: 200, body: { offers, recommended_quote_id: "q2" } },
    }).saved,
    { quote_id: "q2", number_type: "mobile", expected_total_cents: 350 },
  );
  for (const [name, key] of [
    ["Get a quoted number", "number_id"],
    ["Create an assistant", "agent_id"],
    ["Register my own domain", "domain_id"],
    ["Send a test email", "email_id"],
  ]) {
    assert.deepEqual(
      run(name, "test", { response: { code: 201, body: { id: "x" } } }).saved,
      { [key]: "x" },
    );
    assert.deepEqual(
      run(name, "test", { response: { code: 422, body: {} } }).saved,
      {},
    );
  }
  assert.deepEqual(
    run("List verification submissions", "test", {
      response: { code: 200, body: [{ id: "v1" }] },
    }).saved,
    { verification_id: "v1" },
  );
});

test("a full Start here run cannot spend credit without opting in", () => {
  const paid = [
    "Get a quoted number",
    "Send a test email",
    "Send a test text",
    "Call with my assistant",
  ];
  assert.equal(
    collection.variable.find((v) => v.key === "enable_paid_requests").value,
    "false",
  );
  for (const name of paid) {
    for (const enabled of [undefined, "false", "true"]) {
      const { skipped } = run(name, "prerequest", { enabled });
      assert.equal(skipped, enabled !== "true", `${name}: ${enabled}`);
    }
  }
});

const uid = "12345-12345678-1234-4234-a234-123456789012";
const key = "test-secret-never-send-to-Hail";
const existing = structuredClone(collection);
existing.info._postman_id = uid.slice(6);
existing.item[0].id = "server-folder-id";
// Change a reference request: those have structured URLs.
walk(existing.item.slice(1))[0].id = "server-request-id";
walk(existing.item.slice(1))[0].request.url.raw += "?old_parameter=value";

test("publishing preserves request IDs and only sends credentials to Postman", async () => {
  const calls = [];
  await updateCollection(collection, uid, key, async (url, options) => {
    calls.push({ url, options });
    return {
      ok: true,
      status: 200,
      json: async () =>
        options.method === "PUT"
          ? { collection: { uid } }
          : { collection: existing },
    };
  });
  assert.equal(calls.length, 2);
  for (const { url, options } of calls) {
    assert.equal(url, `https://api.postman.com/collections/${uid}`);
    assert.equal(options.headers["X-Api-Key"], key);
  }
  const sent = JSON.parse(calls[1].options.body).collection;
  assert.equal(sent.info._postman_id, existing.info._postman_id);
  assert.equal(sent.item[0].id, "server-folder-id");
  assert.equal(walk(sent.item.slice(1))[0].id, "server-request-id");
  assert.ok(!calls[1].options.body.includes(key));
});

test("publishing refuses wrong targets and reports failed or queued updates", async () => {
  let writes = 0;
  const wrong = async (_, options) => {
    if (options.method === "PUT") writes++;
    return {
      ok: true,
      status: 200,
      json: async () => ({ collection: { info: { name: "Another product" } } }),
    };
  };
  await assert.rejects(
    updateCollection(collection, uid, key, wrong),
    /refusing to replace/,
  );
  await assert.rejects(
    updateCollection(collection, "https://example.com", key, wrong),
    /valid Postman collection UID/,
  );
  assert.equal(writes, 0);
  for (const status of [202, 401, 429, 500]) {
    const failed = async (_, options) =>
      options.method === "PUT"
        ? { status, ok: status < 400 }
        : {
            ok: true,
            status: 200,
            json: async () => ({ collection: existing }),
          };
    await assert.rejects(
      updateCollection(collection, uid, key, failed),
      new RegExp(`HTTP ${status}`),
    );
  }
});
