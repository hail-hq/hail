import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Chat, NotImplementedError } from "chat";
import { createMemoryState } from "@chat-adapter/state-memory";
import { createHailAdapter } from "../src/index.js";
const from = "+14155550100",
  peer = "+14155550200",
  secret = "whsec_test";
const threadId = "hail:+14155550100:+14155550200",
  id = "fd658fd6-50ea-4248-acb1-3023bd51da0c";
const sms = {
  id,
  organization_id: "org-test",
  from_e164: from,
  to_e164: peer,
  direction: "outbound",
  agent_id: null,
  status: "sent",
  body: "Hello",
  provider_message_sid: "SM-test",
  segment_count: 1,
  error_code: null,
  requested_at: "2026-11-10T12:00:00Z",
  sent_at: "2026-11-10T12:00:01Z",
};
const event = {
  id: "delivery-test",
  type: "sms.received",
  api_version: "2026-06-06",
  organization_id: "org-test",
  created_at: "2026-11-10T12:00:00Z",
  data: { id, from: peer, to: from, body: "*literal*\nHello" },
};
function signed(
  body = JSON.stringify(event),
  timestamp = Math.floor(Date.now() / 1000),
  key = secret,
) {
  const mac = createHmac("sha256", key)
    .update(`${timestamp}.`)
    .update(body)
    .digest("hex");
  return new Request("https://bot.example/api/webhooks/hail", {
    method: "POST",
    body,
    headers: { "x-hail-signature": `t=${timestamp},v1=${mac}` },
  });
}
function adapter(options: Record<string, unknown> = {}) {
  return createHailAdapter({
    apiKey: "test-key",
    fromNumber: from,
    webhookSecret: secret,
    recipientConsent: true,
    ...options,
  });
}
const bots: Chat[] = [];
afterEach(async () => {
  await Promise.all(bots.splice(0).map((bot) => bot.shutdown()));
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("webhooks", () => {
  it.each([
    [
      "missing",
      () =>
        new Request("https://bot.example", {
          method: "POST",
          body: JSON.stringify(event),
        }),
    ],
    ["wrong secret", () => signed(undefined, undefined, "wrong")],
    ["stale", () => signed(undefined, Math.floor(Date.now() / 1000) - 301)],
    ["future", () => signed(undefined, Math.floor(Date.now() / 1000) + 301)],
    [
      "short digest",
      () =>
        new Request("https://bot.example", {
          method: "POST",
          body: "{}",
          headers: { "x-hail-signature": "t=1,v1=00" },
        }),
    ],
    [
      "changed body",
      () =>
        new Request(signed(), {
          body: JSON.stringify({
            ...event,
            data: { ...event.data, body: "tampered" },
          }),
        }),
    ],
  ] as const)(
    "rejects %s signatures before processing",
    async (_label, request) => {
      expect((await adapter().handleWebhook(request())).status).toBe(401);
    },
  );
  it("fails closed without a secret", async () => {
    vi.stubEnv("HAIL_WEBHOOK_SECRET", "");
    expect(
      (await adapter({ webhookSecret: undefined }).handleWebhook(signed()))
        .status,
    ).toBe(503);
  });
  it("rejects signed malformed payloads", async () => {
    expect((await adapter().handleWebhook(signed("{"))).status).toBe(400);
    expect(
      (
        await adapter().handleWebhook(
          signed(
            JSON.stringify({ ...event, data: { ...event.data, from: "bad" } }),
          ),
        )
      ).status,
    ).toBe(400);
  });
  it("ignores other events and other Hail numbers", async () => {
    expect(
      (
        await adapter().handleWebhook(
          signed(JSON.stringify({ ...event, type: "sms.delivered" })),
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await adapter().handleWebhook(
          signed(
            JSON.stringify({
              ...event,
              data: { ...event.data, to: "+14155550300" },
            }),
          ),
        )
      ).status,
    ).toBe(200);
  });
  it("accepts the Python signer fixture in Hail docs", async () => {
    vi.spyOn(Date, "now").mockReturnValue(1700000000000);
    const request = new Request("https://bot.example", {
      method: "POST",
      body: '{"id":"evt_123","type":"email.received","data":{"id":"em_1"}}',
      headers: {
        "x-hail-signature":
          "t=1700000000,v1=72f7940d13ca8528cf655a04e82dabfe90a531e239241fda9bdff72980d33a4a",
      },
    });
    expect(
      (await adapter({ webhookSecret: "whsec_example" }).handleWebhook(request))
        .status,
    ).toBe(200);
  });
  it("routes literal SMS through real Chat, replies, stores history, and deduplicates retries", async () => {
    const sent: unknown[] = [];
    const hail = adapter({
      fetch: async (url: URL, init: RequestInit) => {
        sent.push({ url: String(url), body: JSON.parse(String(init.body)) });
        return Response.json(sms);
      },
    });
    const bot = new Chat({
      userName: "test",
      adapters: { hail },
      state: createMemoryState(),
    });
    bots.push(bot);
    const received: string[] = [];
    bot.onDirectMessage(async (thread, message) => {
      received.push(message.text);
      expect(message.threadId).toBe(threadId);
      expect(message.author.userId).toBe(peer);
      expect(message.metadata.dateSent.toISOString()).toBe(
        "2026-11-10T12:00:00.000Z",
      );
      await thread.post("Hello");
    });
    expect((await bot.webhooks.hail(signed())).status).toBe(200);
    expect((await bot.webhooks.hail(signed())).status).toBe(200);
    expect(received).toEqual(["*literal*\nHello"]);
    expect(sent).toEqual([
      {
        url: "https://api.hail.so/v1/sms",
        body: {
          from,
          to: peer,
          body: "Hello",
          recipient_consent: true,
          message_type: "informational",
        },
      },
    ]);
    const history: string[] = [];
    for await (const message of bot.thread(threadId).messages)
      history.push(message.text);
    expect(history).toContain("*literal*\nHello");
    expect(history).toContain("Hello");
  });
});
describe("background processing", () => {
  it("acknowledges a signed webhook before a slow handler finishes when waitUntil is supplied", async () => {
    let release!: () => void;
    let entered!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const bot = new Chat({
      userName: "test",
      adapters: { hail: adapter() },
      state: createMemoryState(),
    });
    bots.push(bot);
    bot.onDirectMessage(async () => {
      entered();
      await gate;
    });
    const tasks: Promise<unknown>[] = [];
    const response = bot.webhooks.hail(signed(), {
      waitUntil: (task) => {
        tasks.push(task);
      },
    });
    await started;
    try {
      const status = await Promise.race([
        response.then((result) => result.status),
        new Promise<string>((resolve) =>
          setTimeout(() => resolve("blocked"), 100),
        ),
      ]);
      expect(status).toBe(200);
    } finally {
      release();
      await response;
      await Promise.all(tasks);
    }
  });
  it("waits for a handler when no background runtime is supplied", async () => {
    let release!: () => void;
    let entered!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const bot = new Chat({
      userName: "test",
      adapters: { hail: adapter() },
      state: createMemoryState(),
    });
    bots.push(bot);
    bot.onDirectMessage(async () => {
      entered();
      await gate;
    });
    let acknowledged = false;
    const response = bot.webhooks.hail(signed()).then((result) => {
      acknowledged = true;
      return result;
    });
    await started;
    try {
      expect(acknowledged).toBe(false);
    } finally {
      release();
    }
    expect((await response).status).toBe(200);
  });
});

describe("outbound", () => {
  it("resolves credentials per request and refuses redirects", async () => {
    let token = "first";
    const requests: RequestInit[] = [];
    const hail = adapter({
      apiKey: () => token,
      fetch: async (_url: URL, init: RequestInit) => {
        requests.push(init);
        return Response.json(sms);
      },
    });
    expect((await hail.postMessage(threadId, "Hello")).id).toBe(id);
    token = "second";
    await hail.postMessage(threadId, "Hello");
    expect(
      requests.map((init) => new Headers(init.headers).get("authorization")),
    ).toEqual(["Bearer first", "Bearer second"]);
    expect(requests.map((init) => init.redirect)).toEqual(["error", "error"]);
  });
  it("resolves the signing secret for each delivery", async () => {
    let key = secret;
    const hail = adapter({ webhookSecret: () => key });
    const body = JSON.stringify({ ...event, type: "sms.delivered" });
    expect((await hail.handleWebhook(signed(body))).status).toBe(200);
    key = "rotated";
    expect((await hail.handleWebhook(signed(body))).status).toBe(401);
    expect(
      (await hail.handleWebhook(signed(body, undefined, key))).status,
    ).toBe(200);
  });
  it("requires explicit consent before API calls", async () => {
    vi.stubEnv("HAIL_RECIPIENT_CONSENT", "");
    const fetch = vi.fn();
    await expect(
      adapter({ recipientConsent: undefined, fetch }).postMessage(
        threadId,
        "Hello",
      ),
    ).rejects.toThrow(/consent/i);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("checks consent per recipient", async () => {
    const fetch = vi.fn(async () => Response.json(sms));
    const hail = adapter({
      recipientConsent: async (to: string) => to === peer,
      fetch,
    });
    await hail.postMessage(threadId, "Hello");
    await expect(
      hail.postMessage(`hail:${from}:+14155550300`, "Hello"),
    ).rejects.toThrow(/consent/i);
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("rejects attachments, empty text and overlength text before sending", async () => {
    const fetch = vi.fn();
    const hail = adapter({ fetch });
    await expect(
      hail.postMessage(threadId, {
        markdown: "Hello",
        files: [{ data: Buffer.from("image"), filename: "x.png" }],
      }),
    ).rejects.toThrow(/attach|file/i);
    await expect(hail.postMessage(threadId, "x".repeat(1601))).rejects.toThrow(
      /1600/,
    );
    await expect(hail.postMessage(threadId, "")).rejects.toThrow(/empty/);
    expect(fetch).not.toHaveBeenCalled();
  });
  it("does not retry sends or expose API error bodies", async () => {
    const fetch = vi.fn(
      async () => new Response("secret provider details", { status: 429 }),
    );
    await expect(
      adapter({ fetch }).postMessage(threadId, "Hello"),
    ).rejects.toThrow("Hail API request failed (429)");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("uses the versioned self-host API and requires an API key", async () => {
    const urls: string[] = [];
    await adapter({
      apiUrl: "http://localhost:8080/",
      fetch: async (url: URL) => {
        urls.push(String(url));
        return Response.json(sms);
      },
    }).postMessage(threadId, "Hello");
    expect(urls).toEqual(["http://localhost:8080/v1/sms"]);
    vi.stubEnv("HAIL_API_KEY", "");
    await expect(
      adapter({ apiKey: undefined }).postMessage(threadId, "Hello"),
    ).rejects.toThrow(/API key/i);
  });
});
describe("isolation", () => {
  it("validates IDs and prevents sending from other numbers", async () => {
    const hail = adapter();
    expect(hail.encodeThreadId({ fromNumber: from, peerNumber: peer })).toBe(
      threadId,
    );
    expect(hail.decodeThreadId(threadId)).toEqual({
      fromNumber: from,
      peerNumber: peer,
    });
    expect(hail.channelIdFromThreadId(threadId)).toBe(threadId);
    await expect(hail.openDM(peer)).resolves.toBe(threadId);
    for (const invalid of [
      `hail:${from}:bad`,
      `other:${from}:${peer}`,
      `hail:+14155550300:${peer}`,
      `${threadId}:extra`,
      `hail:${from}:${peer}\n`,
    ])
      expect(() => hail.decodeThreadId(invalid)).toThrow();
  });
  it("checks the exact phone pair on single-message reads", async () => {
    await expect(
      adapter({
        fetch: async () => Response.json({ ...sms, to_e164: "+14155550300" }),
      }).fetchMessage(threadId, id),
    ).resolves.toBeNull();
  });
  it("returns null only for 404 and propagates auth errors", async () => {
    await expect(
      adapter({
        fetch: async () => new Response(null, { status: 404 }),
      }).fetchMessage(threadId, id),
    ).resolves.toBeNull();
    await expect(
      adapter({
        fetch: async () => new Response(null, { status: 401 }),
      }).fetchMessage(threadId, id),
    ).rejects.toThrow(/401/);
  });
  it("reports unsupported operations", async () => {
    const hail = adapter();
    for (const method of [
      "editMessage",
      "deleteMessage",
      "addReaction",
      "removeReaction",
    ] as const)
      await expect(hail[method]()).rejects.toBeInstanceOf(NotImplementedError);
  });
});
