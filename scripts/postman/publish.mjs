import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { root } from "./generate.mjs";

// Preserve existing Postman item identities so request links survive updates.
export function preserveIds(next, previous) {
  if (previous?.id) next.id = previous.id;
  const key = (item) =>
    item.request
      ? `${item.request.method} ${(item.request.url.raw ?? item.request.url).split("?")[0]}`
      : `folder:${item.name}`;
  const old = new Map((previous?.item ?? []).map((item) => [key(item), item]));
  for (const item of next.item ?? []) preserveIds(item, old.get(key(item)));
  const responses = new Map(
    (previous?.response ?? []).map((r) => [`${r.code}:${r.name}`, r]),
  );
  for (const response of next.response ?? []) {
    const oldResponse = responses.get(`${response.code}:${response.name}`);
    if (oldResponse?.id) response.id = oldResponse.id;
  }
  return next;
}

export async function updateCollection(
  collection,
  collectionUid,
  apiKey,
  fetcher = fetch,
) {
  // Only accept a collection UUID or Postman UID, never an arbitrary URL.
  if (
    !/^(?:\d+-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      collectionUid ?? "",
    )
  ) {
    throw new Error("Set a valid Postman collection UID");
  }
  if (!apiKey) throw new Error("POSTMAN_API_KEY is required");
  const url = `https://api.postman.com/collections/${collectionUid}`;
  const headers = { "X-Api-Key": apiKey, "Content-Type": "application/json" };
  const existing = await fetcher(url, { headers });
  if (!existing.ok)
    throw new Error(`Read Postman collection failed (HTTP ${existing.status})`);
  const current = (await existing.json()).collection;
  if (current?.info?.name !== collection.info.name) {
    throw new Error(
      `Target must be named ${collection.info.name}; refusing to replace another collection`,
    );
  }
  const next = preserveIds(structuredClone(collection), current);
  next.info._postman_id =
    current.info._postman_id ?? collectionUid.replace(/^\d+-/, "");
  const response = await fetcher(url, {
    method: "PUT",
    headers,
    body: JSON.stringify({ collection: next }),
  });
  // Use synchronous updates. Do not report a queued 202 task as completed.
  if (response.status !== 200)
    throw new Error(
      `Update Postman collection failed (HTTP ${response.status})`,
    );
  return response.json();
}

async function publish() {
  const apiKey = process.env.POSTMAN_API_KEY;
  const targets = [
    ["hail-api-reference", process.env.POSTMAN_REFERENCE_COLLECTION_UID],
    ["hail-start-here", process.env.POSTMAN_QUICKSTART_COLLECTION_UID],
  ];
  if (targets.some(([, uid]) => !uid))
    throw new Error("Both Postman collection UIDs are required");
  for (const [name, uid] of targets) {
    const collection = JSON.parse(
      await readFile(
        new URL(`postman/${name}.postman_collection.json`, root),
        "utf8",
      ),
    );
    await updateCollection(collection, uid, apiKey);
    console.log(`Updated ${collection.info.name}`);
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await publish();
}
