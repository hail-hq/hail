import { createHmac, timingSafeEqual } from "node:crypto";
import {
  Message,
  NotImplementedError,
  stringifyMarkdown,
  type Adapter,
  type AdapterPostableMessage,
  type ChatInstance,
  type FetchOptions,
  type FetchResult,
  type FormattedContent,
  type RawMessage,
  type ThreadInfo,
  type WebhookOptions,
} from "chat";
import type {
  HailAdapterConfig,
  HailCredential,
  HailSms,
  HailThreadId,
} from "./types.js";
export type {
  HailAdapterConfig,
  HailCredential,
  HailSms,
  HailThreadId,
} from "./types.js";

const PHONE = /^\+[1-9][0-9]{6,14}$/;
const SIGNATURE = /^t=([0-9]+),v1=([a-fA-F0-9]{64})$/;
const UUID =
  /^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$/;
function phone(value: unknown): value is string {
  return typeof value === "string" && PHONE.exec(value)?.[0] === value;
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function isSms(value: unknown): value is HailSms {
  return (
    record(value) &&
    typeof value.id === "string" &&
    UUID.test(value.id) &&
    phone(value.from_e164) &&
    phone(value.to_e164) &&
    (value.direction === "inbound" || value.direction === "outbound") &&
    typeof value.body === "string" &&
    typeof value.requested_at === "string" &&
    Number.isFinite(Date.parse(value.requested_at))
  );
}
async function credential(
  value: HailCredential | undefined,
): Promise<string | undefined> {
  return typeof value === "function" ? await value() : value;
}

export class HailApiError extends Error {
  constructor(readonly status: number) {
    super(`Hail API request failed (${status})`);
    this.name = "HailApiError";
  }
}

/** SMS adapter. All conversations are scoped to the configured number and one peer. */
export class HailAdapter implements Adapter<HailThreadId, HailSms> {
  readonly name = "hail";
  readonly lockScope = "thread" as const;
  readonly persistThreadHistory = true;
  readonly userName: string;
  private chat?: ChatInstance;
  private readonly config: HailAdapterConfig;

  constructor(config: HailAdapterConfig = {}) {
    this.config = { ...config };
    this.userName = config.userName ?? "hail-bot";
  }

  private fromNumber(): string {
    const number = this.config.fromNumber ?? process.env.HAIL_FROM_NUMBER;
    if (!phone(number))
      throw new Error(
        "Set HAIL_FROM_NUMBER or fromNumber to a Hail-owned E.164 number",
      );
    return number;
  }

  async initialize(chat: ChatInstance): Promise<void> {
    this.fromNumber();
    this.chat = chat;
  }

  encodeThreadId(thread: HailThreadId): string {
    if (thread.fromNumber !== this.fromNumber() || !phone(thread.peerNumber)) {
      throw new Error("Invalid Hail phone pair");
    }
    return `hail:${thread.fromNumber}:${thread.peerNumber}`;
  }

  decodeThreadId(threadId: string): HailThreadId {
    const [name, fromNumber, peerNumber, extra] = threadId.split(":");
    if (
      name !== this.name ||
      extra !== undefined ||
      fromNumber !== this.fromNumber() ||
      !phone(peerNumber)
    ) {
      throw new Error("Invalid Hail thread ID");
    }
    return { fromNumber, peerNumber };
  }

  channelIdFromThreadId(threadId: string): string {
    this.decodeThreadId(threadId);
    return threadId;
  }

  isDM(threadId: string): boolean {
    this.decodeThreadId(threadId);
    return true;
  }

  async openDM(peerNumber: string): Promise<string> {
    return this.encodeThreadId({ fromNumber: this.fromNumber(), peerNumber });
  }

  async fetchThread(threadId: string): Promise<ThreadInfo> {
    const thread = this.decodeThreadId(threadId);
    return {
      id: threadId,
      channelId: threadId,
      channelName: thread.peerNumber,
      channelVisibility: "private",
      isDM: true,
      metadata: { ...thread },
    };
  }

  async handleWebhook(
    request: Request,
    options?: WebhookOptions,
  ): Promise<Response> {
    if (request.method !== "POST")
      return new Response(null, { status: 405, headers: { Allow: "POST" } });
    const secret = await credential(
      this.config.webhookSecret ?? process.env.HAIL_WEBHOOK_SECRET,
    );
    if (!secret)
      return new Response("Webhook secret is not configured", { status: 503 });
    const body = new Uint8Array(await request.arrayBuffer());
    const match = SIGNATURE.exec(request.headers.get("x-hail-signature") ?? "");
    if (
      !match ||
      !Number.isSafeInteger(Number(match[1])) ||
      Math.abs(Date.now() / 1000 - Number(match[1])) > 300
    ) {
      return new Response("Invalid signature", { status: 401 });
    }
    const expected = createHmac("sha256", secret)
      .update(`${match[1]}.`)
      .update(body)
      .digest();
    if (!timingSafeEqual(expected, Buffer.from(match[2], "hex")))
      return new Response("Invalid signature", { status: 401 });
    let event: unknown;
    try {
      event = JSON.parse(new TextDecoder().decode(body));
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }
    if (!record(event) || typeof event.type !== "string")
      return new Response("Invalid event", { status: 400 });
    if (event.type !== "sms.received")
      return new Response(null, { status: 200 });
    const data = event.data;
    if (
      !record(data) ||
      typeof data.id !== "string" ||
      !UUID.test(data.id) ||
      !phone(data.from) ||
      !phone(data.to) ||
      typeof data.body !== "string" ||
      typeof event.created_at !== "string" ||
      !Number.isFinite(Date.parse(event.created_at))
    ) {
      return new Response("Invalid SMS event", { status: 400 });
    }
    if (data.to !== this.fromNumber())
      return new Response(null, { status: 200 });
    if (!this.chat)
      return new Response("Adapter is not initialized", { status: 503 });
    const message = this.parseMessage({
      id: data.id,
      from_e164: data.from,
      to_e164: data.to,
      direction: "inbound",
      body: data.body,
      requested_at: event.created_at,
      status: "received",
    });
    const task = this.chat.processMessage(
      this,
      message.threadId,
      message,
      options,
    );
    // Chat registers the task with waitUntil; the webhook must not await it too.
    if (!options?.waitUntil) await task;
    return new Response(null, { status: 200 });
  }

  parseMessage(raw: HailSms): Message<HailSms> {
    if (!isSms(raw)) throw new Error("Invalid Hail SMS response");
    const inbound = raw.direction === "inbound";
    const threadId = this.encodeThreadId({
      fromNumber: inbound ? raw.to_e164 : raw.from_e164,
      peerNumber: inbound ? raw.from_e164 : raw.to_e164,
    });
    return new Message({
      id: raw.id,
      threadId,
      text: raw.body,
      // SMS is literal text. Do not interpret Markdown typed by the sender.
      formatted: {
        type: "root",
        children: [
          { type: "paragraph", children: [{ type: "text", value: raw.body }] },
        ],
      },
      raw,
      attachments: [],
      isMention: inbound,
      author: {
        userId: raw.from_e164,
        userName: inbound ? raw.from_e164 : this.userName,
        fullName: inbound ? raw.from_e164 : this.userName,
        isBot: !inbound,
        isMe: !inbound,
      },
      metadata: { dateSent: new Date(raw.requested_at), edited: false },
    });
  }

  renderFormatted(content: FormattedContent): string {
    return stringifyMarkdown(content).trim();
  }

  private text(message: AdapterPostableMessage): string {
    if (typeof message === "string") return message;
    if (
      ("files" in message && message.files?.length) ||
      ("attachments" in message &&
        Array.isArray(message.attachments) &&
        message.attachments.length)
    ) {
      throw new Error("Hail SMS does not support files or attachments");
    }
    if ("markdown" in message) return message.markdown;
    if ("raw" in message) return message.raw;
    if ("ast" in message) return this.renderFormatted(message.ast);
    throw new NotImplementedError(
      "Hail SMS supports text messages",
      "postMessage",
    );
  }

  private async api(path: string, body?: unknown): Promise<unknown> {
    const apiKey = await credential(
      this.config.apiKey ?? process.env.HAIL_API_KEY,
    );
    if (!apiKey)
      throw new Error("An API key is required: set HAIL_API_KEY or apiKey");
    const origin = new URL(
      this.config.apiUrl ?? process.env.HAIL_API_URL ?? "https://api.hail.so",
    );
    if (
      !["https:", "http:"].includes(origin.protocol) ||
      origin.username ||
      origin.password ||
      origin.search ||
      origin.hash ||
      origin.pathname !== "/"
    ) {
      throw new Error(
        "apiUrl must be an HTTP(S) origin without a path, credentials, query, or fragment",
      );
    }
    const response = await (this.config.fetch ?? globalThis.fetch)(
      new URL(path, origin),
      {
        method: body === undefined ? "GET" : "POST",
        redirect: "error",
        signal: AbortSignal.timeout(30_000),
        headers: {
          Authorization: `Bearer ${apiKey}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      },
    );
    if (!response.ok) throw new HailApiError(response.status);
    return await response.json();
  }

  async postMessage(
    threadId: string,
    message: AdapterPostableMessage,
  ): Promise<RawMessage<HailSms>> {
    const { fromNumber, peerNumber } = this.decodeThreadId(threadId);
    const body = this.text(message);
    if (!body.trim()) throw new Error("SMS text cannot be empty");
    if ([...body].length > 1600)
      throw new Error("SMS text must be at most 1600 characters");
    const consent =
      this.config.recipientConsent ??
      process.env.HAIL_RECIPIENT_CONSENT === "true";
    if (
      (typeof consent === "function" ? await consent(peerNumber) : consent) !==
      true
    ) {
      throw new Error(
        "Explicit recipient consent is required before sending SMS",
      );
    }
    const raw = await this.api("/v1/sms", {
      from: fromNumber,
      to: peerNumber,
      body,
      recipient_consent: true,
      message_type: "informational",
    });
    if (
      !isSms(raw) ||
      raw.from_e164 !== fromNumber ||
      raw.to_e164 !== peerNumber ||
      raw.direction !== "outbound" ||
      raw.status === "failed" ||
      raw.status === "undelivered"
    ) {
      throw new Error("Unexpected Hail SMS send response");
    }
    return { id: raw.id, threadId, raw };
  }

  async fetchMessage(
    threadId: string,
    messageId: string,
  ): Promise<Message<HailSms> | null> {
    this.decodeThreadId(threadId);
    if (!UUID.test(messageId)) throw new Error("Invalid Hail message ID");
    let raw: unknown;
    try {
      raw = await this.api(`/v1/sms/${encodeURIComponent(messageId)}`);
    } catch (error) {
      if (error instanceof HailApiError && error.status === 404) return null;
      throw error;
    }
    if (!isSms(raw)) throw new Error("Invalid Hail SMS response");
    const { fromNumber, peerNumber } = this.decodeThreadId(threadId);
    if (
      raw.direction === "inbound"
        ? raw.to_e164 !== fromNumber || raw.from_e164 !== peerNumber
        : raw.from_e164 !== fromNumber || raw.to_e164 !== peerNumber
    )
      return null;
    return this.parseMessage(raw);
  }

  /** Return no native history so Chat SDK uses its persisted thread-history fallback. */
  async fetchMessages(
    threadId: string,
    _options?: FetchOptions,
  ): Promise<FetchResult<HailSms>> {
    this.decodeThreadId(threadId);
    return { messages: [] };
  }
  async editMessage(): Promise<RawMessage<HailSms>> {
    throw new NotImplementedError(
      "SMS messages cannot be edited",
      "editMessage",
    );
  }
  async deleteMessage(): Promise<void> {
    throw new NotImplementedError(
      "SMS messages cannot be deleted",
      "deleteMessage",
    );
  }
  async addReaction(): Promise<void> {
    throw new NotImplementedError("SMS has no reactions", "addReaction");
  }
  async removeReaction(): Promise<void> {
    throw new NotImplementedError("SMS has no reactions", "removeReaction");
  }
  async startTyping(): Promise<void> {
    /* SMS has no typing indicator. */
  }
}

export function createHailAdapter(config: HailAdapterConfig = {}): HailAdapter {
  return new HailAdapter(config);
}
