# Hail adapter for Chat SDK

Send and receive SMS through [Vercel Chat SDK](https://chat-sdk.dev) using your Hail phone number.
Requires Node.js 20+ and `chat ^4.44.0`. Hail maintains this adapter in its official repository.

## Install

```bash
pnpm add @hail-hq/chat-sdk-adapter chat @chat-adapter/state-memory
```

## Quick start

```typescript
// lib/bot.ts
import { Chat } from "chat";
import { createMemoryState } from "@chat-adapter/state-memory";
import { createHailAdapter } from "@hail-hq/chat-sdk-adapter";

export const bot = new Chat({
  userName: "mybot",
  adapters: { hail: createHailAdapter() },
  state: createMemoryState(),
});

bot.onDirectMessage(async (thread, message) => {
  await thread.post(`You said: ${message.text}`);
});
```

```typescript
// app/api/webhooks/hail/route.ts
import { after } from "next/server";
import { bot } from "@/lib/bot";

export const runtime = "nodejs";
export async function POST(request: Request): Promise<Response> {
  return bot.webhooks.hail(request, { waitUntil: (task) => after(() => task) });
}
```

Use a persistent state adapter such as `@chat-adapter/state-redis` in production.
The [Next.js example](examples/nextjs) uses memory for local development and Redis when `REDIS_URL` is set.
The adapter uses Node's crypto module and does not support the Edge runtime.

## Hail setup

1. Create an API key in [Hail API keys](https://hail.so/console/keys).
2. Use a Hail-owned, SMS-capable phone number. Set `HAIL_FROM_NUMBER` to its E.164 number, for example `+14155550100`.
3. If the number has a Hail text agent, clear its `sms_agent_id` before using this bot. Otherwise both can reply. Refer to [number routing](https://hail.so/docs/agents).
4. Create a webhook subscription for `sms.received` pointing to your deployed route:

   ```bash
   curl https://api.hail.so/v1/webhooks \
     -H "Authorization: Bearer $HAIL_API_KEY" \
     -H 'Content-Type: application/json' \
     -d '{"target_url":"https://your-bot.example/api/webhooks/hail","event_types":["sms.received"]}'
   ```

5. Save the response's once-shown `secret` as `HAIL_WEBHOOK_SECRET`.
6. Set `HAIL_RECIPIENT_CONSENT=true` only when you can attest to the required consent for every recipient this bot contacts. Without it, replies fail before calling Hail.

Replies incur Hail's outbound SMS charges and require workspace credit. Carrier number and registration requirements still apply, including US A2P registration for US long-code texting.

## Configuration

`createHailAdapter(config?)` accepts:

| Option             | Type                                                       | Default                                    |
| ------------------ | ---------------------------------------------------------- | ------------------------------------------ |
| `apiKey`           | `string \| (() => string \| Promise<string>)`              | `HAIL_API_KEY`                             |
| `webhookSecret`    | `string \| (() => string \| Promise<string>)`              | `HAIL_WEBHOOK_SECRET`                      |
| `fromNumber`       | `string`                                                   | `HAIL_FROM_NUMBER`                         |
| `apiUrl`           | `string`                                                   | `HAIL_API_URL`, then `https://api.hail.so` |
| `recipientConsent` | `boolean \| ((to: string) => boolean \| Promise<boolean>)` | `HAIL_RECIPIENT_CONSENT === "true"`        |
| `userName`         | `string`                                                   | `"hail-bot"`                               |
| `fetch`            | `typeof fetch`                                             | `globalThis.fetch`                         |

Credentials resolve on every API call or webhook delivery. They are never cached:

```typescript
createHailAdapter({
  apiKey: async () => await getCurrentHailToken(),
  webhookSecret: async () => await getCurrentWebhookSecret(),
  recipientConsent: async (to) => await hasConsentFor(to),
});
```

The example above assumes your application defines the token, secret, and consent functions.
Credential resolvers support rotating or short-lived tokens; they do not register Hail with Vercel Connect.
`apiUrl` must be an HTTP(S) origin without `/v1`, credentials, query, or fragment.
Use HTTPS for remote servers. Requests have a 30-second timeout and reject redirects.

## Behavior and feature support

- Incoming `sms.received` events trigger `onDirectMessage`, or `onNewMention` when no direct-message handler exists. Sender text stays literal.
- `thread.post()` sends informational SMS. Strings and raw text remain unchanged; Markdown and AST content are sent as text with Markdown syntax. Empty messages and messages over 1,600 Unicode code points fail before sending.
- `openDM(peerNumber)` returns `hail:{yourNumber}:{peerNumber}`. The configured Hail number and the peer scope both threads and channels, isolating state between conversations.
- Every webhook requires a valid HMAC-SHA256 `X-Hail-Signature` over the original bytes and a timestamp within five minutes. Other event types and texts to other Hail numbers are acknowledged without running handlers.
- Chat SDK deduplicates message IDs within its configured `dedupeTtlMs`. Hail retries can outlive this window; do not assume exactly-once processing. The adapter does not retry outbound sends automatically.
- Chat SDK persists observed inbound and outbound messages in your state adapter. `thread.messages` uses this cache; older workspace messages are not backfilled. `fetchMessages()` returns no native history, enabling the SDK fallback. `fetchMessage()` reads a message by UUID and checks that it belongs to the exact phone pair; only `404` becomes `null`.
- SMS has no typing indicator, so `startTyping()` is a no-op. Editing, deletion, and reactions throw `NotImplementedError`. Files, attachments, and cards are rejected. MMS, iMessage, RCS, voice, email, rich controls, and native streaming are outside this adapter's scope.
- Failed HTTP responses throw `HailApiError` with a numeric `status`. Response bodies are excluded from errors. Network and timeout errors propagate. A timeout does not prove that a chargeable send failed; reconcile against Hail before retrying manually.

Refer to [Hail webhook signing and retries](https://hail.so/docs/webhooks).

## Develop and release

```bash
cd integrations/chat-sdk
pnpm install --frozen-lockfile
pnpm test
pnpm typecheck
pnpm build
npm pack --dry-run
```

This directory is an independent pnpm workspace. It does not alter Hail's service workspace or deploy services.
Refer to [RELEASING.md](RELEASING.md) for publication and Vercel submission gates.

## License and support

AGPL-3.0-only, matching the Hail repository. Report adapter bugs in [Hail issues](https://github.com/hail-hq/hail/issues).
