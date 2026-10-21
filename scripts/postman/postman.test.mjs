import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { generate, root, walk } from "./generate.mjs";
import { updateCollection } from "./publish.mjs";
import { resolve } from "./reference.mjs";

const { reference, quickstart, spec } = await generate();

test("quickstart requests use current public routes, fields, and query parameters", () => {
  for (const { request } of walk(quickstart.item)) {
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

test("exports have unique stable IDs and no internal requests or fake auth headers", async () => {
  const repeated = await generate();
  for (const [name, collection] of [
    ["reference", reference],
    ["quickstart", quickstart],
  ]) {
    const items = walk(collection.item);
    assert.equal(new Set(items.map((i) => i.id)).size, items.length);
    assert.deepEqual(
      items.map((i) => i.id),
      walk(repeated[name].item).map((i) => i.id),
    );
    assert.equal(collection.auth.bearer[0].value, "{{api_key}}");
    assert.equal(
      collection.variable.find((v) => v.key === "api_key").value,
      "{{vault:hail-api-key}}",
    );
    for (const { request } of items) {
      const url = request.url.raw ?? request.url;
      assert.doesNotMatch(url, /\/(internal|admin|superadmin)(\/|$)/);
      assert.ok(
        !request.header.some((h) => h.key.toLowerCase() === "authorization"),
      );
    }
  }
  assert.match(reference.info.description, /https:\/\/hail.so\/signup/);
  const source = await readFile(new URL("postman/README.md", root), "utf8");
  assert.equal(reference.info.description, source);
});

test("a full quickstart run cannot spend credit without opting in", () => {
  const paid = [
    "Get a quoted number",
    "Send a test email",
    "Send a test text",
    "Call with my assistant",
  ];
  assert.equal(
    quickstart.variable.find((v) => v.key === "enable_paid_requests").value,
    "false",
  );
  for (const name of paid) {
    const script = walk(quickstart.item)
      .find((i) => i.name === name)
      .event[0].script.exec.join("\n");
    for (const enabled of [undefined, "false", "true"]) {
      let skipped = false;
      new Function("pm", "console", script)(
        {
          variables: { get: () => enabled },
          execution: {
            skipRequest: () => {
              skipped = true;
            },
          },
        },
        { warn() {} },
      );
      assert.equal(skipped, enabled !== "true", `${name}: ${enabled}`);
    }
  }
});

const uid = "12345-12345678-1234-4234-a234-123456789012";
const key = "test-secret-never-send-to-Hail";
const existing = structuredClone(reference);
existing.info._postman_id = uid.slice(6);
existing.item[0].id = "server-folder-id";
walk(existing.item)[0].id = "server-request-id";
walk(existing.item)[0].request.url.raw += "?old_parameter=value";

test("publishing preserves request IDs and only sends credentials to Postman", async () => {
  const calls = [];
  await updateCollection(reference, uid, key, async (url, options) => {
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
  assert.equal(walk(sent.item)[0].id, "server-request-id");
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
    updateCollection(reference, uid, key, wrong),
    /refusing to replace/,
  );
  await assert.rejects(
    updateCollection(reference, "https://example.com", key, wrong),
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
      updateCollection(reference, uid, key, failed),
      new RegExp(`HTTP ${status}`),
    );
  }
});
