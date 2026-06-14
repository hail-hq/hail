# Inbound Email Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Git policy:** Never run git write commands. "Commit checkpoint" steps are user-facing only.
>
> **Repo:** all paths are in `hail-website/` unless noted. Run tests with `pnpm vitest run`, typecheck with `pnpm tsc --noEmit`, build with `pnpm build`. Read each component named in a task before editing it and **match its existing markup/className conventions** (`c-drawer-*`, `lbl`, `it`, `grp`, etc.) — do not invent a new visual style; this is an existing brutalist/editorial console.

**Goal:** Make inbound email self-serve in the console — read received mail (attachments, thread, verdicts behind an "Advanced" disclosure), configure forwarding/webhooks per address, manage org-wide webhook subscriptions, and see why a forward was suppressed.

**Architecture:** Reads go directly against shared Postgres via `pool.query` (existing pattern). All mutations, test-sends, and presigned-URL fetches call the existing **public** Hail API as the org — the console is just another API client, which is what keeps the self-hosted ↔ managed split clean (no console-specific backend code). Auth is a **full-scope org API key**, reused per browser via an HTTP-only cookie and re-minted on 401 (the proven `app/actions/place-call.ts` pattern, generalized into `lib/hail-api.ts`). The key is **visible and revocable** in `/console/keys`; rename it from `UI calls · <date>` to `Used by Console · <date>` since it now covers more than calls. No new `/internal` endpoints, no JWT, no DB side-doors for writes.

**Tech Stack:** Next.js (App Router, server actions), TypeScript, vitest.

Spec: `docs/superpowers/specs/2026-06-13-inbound-email-console-design.md`

---

### Task 1: Generalize the org-scoped API caller into `lib/hail-api.ts`

Today `app/actions/place-call.ts` holds `hailFetch` + the UI-key cookie ensure/mint-on-401 logic privately. Extract a reusable caller so webhooks/domain actions and the presign action share one proven path.

**Files:**

- Create: `lib/hail-api.ts`
- Modify: `app/actions/place-call.ts` (import the extracted helper)
- Test: `lib/__tests__/hail-api.test.ts`

- [ ] **Step 1: Read** `app/actions/place-call.ts` (the `requireApiUrl`, `hailFetch`, cookie-ensure, mint-on-401 retry) and `lib/ui-call-key-cookie.ts`. Identify the exact ensure/mint/retry sequence.

- [ ] **Step 2: Write the failing test** — `lib/__tests__/hail-api.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/ui-call-key-cookie", () => ({
  readUiCallKeyCookie: vi.fn(),
  writeUiCallKeyCookie: vi.fn(),
}));
vi.mock("@/lib/api-keys", () => ({ mintUiKey: vi.fn() }));

import { callHailApiAsOrg } from "@/lib/hail-api";
import { readUiCallKeyCookie } from "@/lib/ui-call-key-cookie";
import { mintUiKey } from "@/lib/api-keys";

describe("callHailApiAsOrg", () => {
  beforeEach(() => {
    process.env.HAIL_API_URL = "https://api.test";
    vi.restoreAllMocks();
  });

  it("uses the cookied key and returns the response", async () => {
    (readUiCallKeyCookie as any).mockResolvedValue("hail_existing");
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const resp = await callHailApiAsOrg("/webhooks", { method: "GET" });
    expect(resp.status).toBe(200);
    expect(fetchMock.mock.calls[0][1].headers.authorization).toBe(
      "Bearer hail_existing",
    );
  });

  it("mints a fresh key and retries once on 401", async () => {
    (readUiCallKeyCookie as any).mockResolvedValue("hail_revoked");
    (mintUiKey as any).mockResolvedValue("hail_fresh");
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("", { status: 401 }))
      .mockResolvedValueOnce(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const resp = await callHailApiAsOrg("/webhooks", {
      method: "POST",
      body: { x: 1 },
    });
    expect(resp.status).toBe(200);
    expect(fetchMock.mock.calls[1][1].headers.authorization).toBe(
      "Bearer hail_fresh",
    );
  });
});
```

(Adjust the mocked helper/function names — `readUiCallKeyCookie`, `writeUiCallKeyCookie`, and whatever `place-call.ts` calls to mint — to the real exports you found in Step 1.)

- [ ] **Step 3: Run** → FAIL (`No module named hail-api`).

- [ ] **Step 4: Implement** `lib/hail-api.ts` — move `requireApiUrl` + the fetch/auth/mint-on-401 logic here as `callHailApiAsOrg(path, { method, body? })`. It: reads the cookied UI key, calls the API with `Authorization: Bearer <key>`, on 401 mints a fresh key (existing mint fn), writes the cookie, retries once. Set `content-type: application/json` + `idempotency-key` only when `body` is present, same as today. `cache: "no-store"`. Then update `app/actions/place-call.ts` to import `callHailApiAsOrg`/`requireApiUrl` from `lib/hail-api.ts` instead of its local copies (delete the now-duplicated locals).

- [ ] **Step 5: Rename the credential.** The minted key is named `UI calls · <date>` (in `app/actions/place-call.ts` / the mint helper, ~line 29). Change the name string to `Used by Console · <date>` since it now backs all dashboard actions, not just calls. Keep it visible and revocable in `/console/keys` (no hiding); the cookie reuse + mint-on-401 behavior is unchanged, so revoking it just causes a fresh `Used by Console · <date>` mint on the next action.

- [ ] **Step 6: Run** `pnpm vitest run lib/__tests__/hail-api.test.ts` → PASS; `pnpm tsc --noEmit` clean; confirm place-call still typechecks.

- [ ] **Step 7: Commit checkpoint (user).** `refactor(console): generalize dashboard API caller; rename UI key to "Used by Console"`

---

### Task 2: Extend `getEmailDetail` for inbound fields + attachments

**Files:**

- Modify: `lib/activity-queries.ts` (the `EmailDetail` type ~line 5 of its block, and `getEmailDetail` ~line 360)
- Test: `lib/__tests__/email-detail.test.ts`

- [ ] **Step 1: Read** the `EmailDetail` type and `getEmailDetail` in `lib/activity-queries.ts` (current SELECT pulls `id, from_address, to_addresses, cc_addresses, subject, body_text, body_html, status, end_reason, provider, provider_message_id, requested_at, sent_at, failed_at`).

- [ ] **Step 2: Write the failing test** — `lib/__tests__/email-detail.test.ts` (mock `pool` to assert the query selects the new columns and maps them):

```ts
import { describe, it, expect, vi } from "vitest";

const query = vi.fn();
vi.mock("@/lib/db", () => ({
  pool: { query: (...a: unknown[]) => query(...a) },
}));

import { getEmailDetail } from "@/lib/activity-queries";

it("returns inbound fields + attachments", async () => {
  query
    .mockResolvedValueOnce({
      rows: [
        {
          id: "e1",
          from_address: "a@x",
          to_addresses: ["u+o@mail.hail.so"],
          cc_addresses: null,
          subject: "hi",
          body_text: "b",
          body_html: null,
          status: "received",
          end_reason: null,
          provider: "ses",
          provider_message_id: "p1",
          requested_at: new Date(),
          sent_at: null,
          failed_at: null,
          direction: "inbound",
          spam_verdict: "PASS",
          virus_verdict: "PASS",
          spf_verdict: "PASS",
          dkim_verdict: "PASS",
          dmarc_verdict: "FAIL",
          message_id: "<m1>",
          in_reply_to: null,
          references_ids: null,
          raw_s3_key: "raw/e1",
        },
      ],
    })
    .mockResolvedValueOnce({
      rows: [
        {
          id: "a1",
          filename: "invoice.pdf",
          content_type: "application/pdf",
          size_bytes: 1234,
        },
      ],
    });
  const d = await getEmailDetail("org1", "e1");
  expect(d?.direction).toBe("inbound");
  expect(d?.dmarcVerdict).toBe("FAIL");
  expect(d?.attachments?.[0].filename).toBe("invoice.pdf");
});
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement.** Extend the row type + SELECT to add `direction, spam_verdict, virus_verdict, spf_verdict, dkim_verdict, dmarc_verdict, message_id, in_reply_to, references_ids, raw_s3_key`. Add a second query for attachments:

```sql
SELECT id, filename, content_type, size_bytes
  FROM email_attachments WHERE email_id = $1 ORDER BY created_at
```

Extend the `EmailDetail` type with: `direction: string`, `spamVerdict/virusVerdict/spfVerdict/dkimVerdict/dmarcVerdict: string | null`, `messageId: string | null`, `inReplyTo: string | null`, `referencesIds: string[] | null`, `rawS3Key: string | null`, `attachments: { id: string; filename: string; contentType: string; sizeBytes: number }[]`. Map all new columns in the return object.

- [ ] **Step 5: Run** → PASS; `pnpm tsc --noEmit` clean.

- [ ] **Step 6: Commit checkpoint (user).** `feat(console): surface inbound fields + attachments in email detail`

---

### Task 3: Presign server action for attachments + raw

**Files:**

- Create/modify: `app/console/activity/actions.ts`
- Test: `app/console/activity/__tests__/presign.test.ts`

- [ ] **Step 1: Write the failing test** — assert the action calls the right API path and returns the 302 `Location`:

```ts
import { describe, it, expect, vi } from "vitest";
vi.mock("@/lib/hail-api", () => ({ callHailApiAsOrg: vi.fn() }));
import {
  getAttachmentUrlAction,
  getRawUrlAction,
} from "@/app/console/activity/actions";
import { callHailApiAsOrg } from "@/lib/hail-api";

it("resolves a presigned attachment URL from the 302 Location", async () => {
  (callHailApiAsOrg as any).mockResolvedValue(
    new Response(null, {
      status: 302,
      headers: { location: "https://s3/...sig" },
    }),
  );
  const url = await getAttachmentUrlAction("e1", "a1");
  expect(url).toBe("https://s3/...sig");
  expect((callHailApiAsOrg as any).mock.calls[0][0]).toBe(
    "/emails/e1/attachments/a1",
  );
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** in `app/console/activity/actions.ts` (mark `"use server"` if the file isn't already a server module):

```ts
import { callHailApiAsOrg } from "@/lib/hail-api";

async function resolveLocation(path: string): Promise<string | null> {
  const resp = await callHailApiAsOrg(path, {
    method: "GET",
    redirect: "manual",
  });
  if (resp.status === 302 || resp.status === 307) {
    return resp.headers.get("location");
  }
  return null;
}

export async function getAttachmentUrlAction(emailId: string, attId: string) {
  return resolveLocation(`/emails/${emailId}/attachments/${attId}`);
}

export async function getRawUrlAction(emailId: string) {
  return resolveLocation(`/emails/${emailId}/raw`);
}
```

Add `redirect?: "manual"` support to `callHailApiAsOrg`'s init in Task 1 if not already passthrough (it forwards to `fetch`). The action returns only the URL string — never the API token.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): server action to presign attachment/raw URLs`

---

### Task 4: Drawer — attachments block + "Advanced" disclosure

**Files:**

- Modify: `app/console/activity/ActivityDrawer.tsx` (the `EmailDetailView` component, ~line 59 renders `<EmailDetailView email={detail.data} />`)
- Test: manual + `pnpm build`

- [ ] **Step 1: Read** `EmailDetailView` and the sibling `CallDetailView` (~line 130) for the established section markup (`c-drawer-hero`, `c-drawer-section`, `c-drawer-tline`, `c-drawer-pre`, `c-drawer-sub`).

- [ ] **Step 2: Implement** in `EmailDetailView`, matching that markup:
  - **Attachments** section (render only when `email.attachments?.length`): a list of `filename · (sizeBytes formatted)`, each a button calling `getAttachmentUrlAction(email.id, att.id)` then `window.open(url)` (client handler; mark the section a small client component if `EmailDetailView` is server-rendered — check how the drawer is wired; the drawer is interactive so it's already client).
  - **Advanced** disclosure (render only when `email.direction === "inbound"`): a native `<details className="c-drawer-advanced"><summary>Advanced</summary>…</details>`, collapsed by default, containing:
    - the five verdict badges (`SPF/DKIM/DMARC/Spam/Virus`) showing `PASS`/`FAIL`/`—`, reusing whatever pill styling the call drawer uses for statuses;
    - a "Download raw .eml" button calling `getRawUrlAction(email.id)` then `window.open`.
  - Leave the existing From/To/Timeline/Body/IDs sections as-is. Verdicts and raw must NOT appear in the main flow — only inside `<details>`.
- [ ] Add minimal CSS for `c-drawer-advanced` in `app/console/console.css` (a quiet, muted disclosure consistent with the existing palette — small caps summary, hairline border). Match existing variables; no new fonts/colors.

- [ ] **Step 3: Verify** — `pnpm build`; manually (or via the existing activity story/screenshot flow) open an inbound row: attachments visible, Advanced collapsed; open an outbound row: no Advanced section.

- [ ] **Step 4: Commit checkpoint (user).** `feat(console): attachments + collapsed Advanced (verdicts/raw) in email drawer`

---

### Task 5: Drawer — thread membership strip

**Files:**

- Modify: `lib/activity-queries.ts` (add `getEmailThread`), `app/console/activity/ActivityDrawer.tsx`
- Test: `lib/__tests__/email-thread.test.ts`

- [ ] **Step 1: Write the failing test** for `getEmailThread(orgId, email)` — given an email with `message_id`/`in_reply_to`/`references_ids`, returns sibling emails in the same org sharing any of those ids:

```ts
import { describe, it, expect, vi } from "vitest";
const query = vi.fn();
vi.mock("@/lib/db", () => ({
  pool: { query: (...a: unknown[]) => query(...a) },
}));
import { getEmailThread } from "@/lib/activity-queries";

it("returns same-org messages sharing message-id lineage", async () => {
  query.mockResolvedValueOnce({
    rows: [
      {
        id: "e1",
        subject: "Re: hi",
        direction: "inbound",
        created_at: new Date(),
      },
      {
        id: "e0",
        subject: "hi",
        direction: "outbound",
        created_at: new Date(),
      },
    ],
  });
  const t = await getEmailThread("org1", {
    messageId: "<m1>",
    inReplyTo: "<m0>",
    referencesIds: ["<m0>"],
  } as any);
  expect(t.map((m) => m.id)).toContain("e0");
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `getEmailThread`: collect the set of ids (`message_id`, `in_reply_to`, each of `references_ids`), then:

```sql
SELECT id, subject, direction, created_at
  FROM emails
 WHERE organization_id = $1
   AND (message_id = ANY($2) OR in_reply_to = ANY($2)
        OR references_ids && $2::text[])
 ORDER BY created_at ASC
 LIMIT 50
```

Return `[]` when the id set is empty. In the drawer, when `> 1` member, render a compact "Thread · N messages" section listing subject + direction + relative time, each linking to that email's drawer (reuse the existing row-open mechanism). Keep it a flat list (no nested threading) per spec.

- [ ] **Step 4: Run** → PASS; `pnpm build`.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): thread membership strip in email drawer`

---

### Task 6: Webhook read queries

**Files:**

- Create: `lib/webhook-queries.ts`
- Test: `lib/__tests__/webhook-queries.test.ts`

- [ ] **Step 1: Write the failing test** — `listSubscriptions(orgId)` and `listDeliveries(orgId, subId)` map rows from `webhook_subscriptions` / `webhook_deliveries` (mock `pool`):

```ts
import { describe, it, expect, vi } from "vitest";
const query = vi.fn();
vi.mock("@/lib/db", () => ({
  pool: { query: (...a: unknown[]) => query(...a) },
}));
import { listSubscriptions, listDeliveries } from "@/lib/webhook-queries";

it("lists subscriptions for the org", async () => {
  query.mockResolvedValueOnce({
    rows: [
      {
        id: "s1",
        target_url: "https://h",
        event_types: ["email.received"],
        status: "active",
        consecutive_failures: 0,
        last_success_at: null,
        last_failure_at: null,
        created_at: new Date(),
        updated_at: new Date(),
      },
    ],
  });
  const subs = await listSubscriptions("org1");
  expect(subs[0].targetUrl).toBe("https://h");
  expect(subs[0].eventTypes).toEqual(["email.received"]);
  expect(query.mock.calls[0][1]).toEqual(["org1"]);
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `lib/webhook-queries.ts` with `listSubscriptions(orgId)` (`WHERE organization_id=$1 ORDER BY created_at DESC`) and `listDeliveries(orgId, subId)` (join through `webhook_subscriptions` to enforce org ownership: `WHERE wd.subscription_id=$2 AND ws.organization_id=$1 ORDER BY wd.created_at DESC LIMIT 100`). Map snake_case → camelCase like `activity-queries.ts` does. Never select secret columns.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): webhook subscription/delivery read queries`

---

### Task 7: Webhook subscription mutations (server actions)

**Files:**

- Create: `app/console/webhooks/actions.ts`
- Test: `app/console/webhooks/__tests__/actions.test.ts`

- [ ] **Step 1: Write the failing test** — each action calls the right public API path via `callHailApiAsOrg`:

```ts
import { describe, it, expect, vi } from "vitest";
vi.mock("@/lib/hail-api", () => ({ callHailApiAsOrg: vi.fn() }));
import {
  createSubscriptionAction,
  rotateSubscriptionSecretAction,
  redeliverAction,
} from "@/app/console/webhooks/actions";
import { callHailApiAsOrg } from "@/lib/hail-api";

it("create posts to /webhooks and returns the once-only secret", async () => {
  (callHailApiAsOrg as any).mockResolvedValue(
    new Response(JSON.stringify({ id: "s1", secret: "whs_x" }), {
      status: 201,
    }),
  );
  const out = await createSubscriptionAction({
    targetUrl: "https://h",
    eventTypes: ["email.received"],
  });
  expect(callHailApiAsOrg).toHaveBeenCalledWith(
    "/webhooks",
    expect.objectContaining({ method: "POST" }),
  );
  expect(out.secret).toBe("whs_x");
});

it("redeliver hits the nested path", async () => {
  (callHailApiAsOrg as any).mockResolvedValue(
    new Response("{}", { status: 200 }),
  );
  await redeliverAction("s1", "d9");
  expect((callHailApiAsOrg as any).mock.calls[0][0]).toBe(
    "/webhooks/s1/deliveries/d9/redeliver",
  );
});
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** `app/console/webhooks/actions.ts` (`"use server"`): `createSubscriptionAction`, `patchSubscriptionAction(id, patch)`, `disableSubscriptionAction(id)` (PATCH status), `rotateSubscriptionSecretAction(id)`, `redeliverAction(subId, deliveryId)`. Each `callHailApiAsOrg` the matching public route (`POST /webhooks`, `PATCH /webhooks/{id}`, `POST /webhooks/{id}/rotate-secret`, `POST /webhooks/{id}/deliveries/{did}/redeliver`); parse JSON, throw on non-2xx with the API error body. Return the `secret` only from create/rotate.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): webhook subscription mutation actions`

---

### Task 8: Webhooks console page

**Files:**

- Create: `app/console/webhooks/page.tsx`, `app/console/webhooks/WebhooksClient.tsx`
- Modify: `app/console/layout.tsx` (nav)
- Test: `pnpm build` + manual

- [ ] **Step 1:** Add a nav link in `app/console/layout.tsx` under WORKSPACE (after "Authorized apps"), matching the existing `<Link className="it" href="...">` markup:

```tsx
<Link className="it" href="/console/webhooks">
  <span>Webhooks</span>
</Link>
```

- [ ] **Step 2:** `app/console/webhooks/page.tsx` (server component): resolve org from session (mirror how `billing/page.tsx` gets the org), `listSubscriptions(orgId)`, render `<WebhooksClient subscriptions={...} />`.

- [ ] **Step 3:** `WebhooksClient.tsx` (client), matching the existing console table/panel markup (read `BillingClient.tsx` / `keys` page for the table + form idiom):
  - Subscriptions table: URL, events, status, consecutive failures, actions (edit, disable, rotate, view deliveries).
  - "Create subscription" form → `createSubscriptionAction`; on success show the returned `secret` once in a copy-to-clipboard callout that disappears on dismiss and is never re-rendered.
  - Rotate → same once-only secret callout.
  - "View deliveries" opens a drawer/expander (Task 9 fills its data).

- [ ] **Step 4: Verify** `pnpm build`; manually load `/console/webhooks`, create a subscription, confirm the secret shows once and isn't present after refresh.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): webhooks management page`

---

### Task 9: Deliveries view + redeliver

**Files:**

- Modify: `app/console/webhooks/page.tsx` (or a nested route/segment), `WebhooksClient.tsx`
- Test: `pnpm build` + manual

- [ ] **Step 1:** Wire the per-subscription deliveries panel to `listDeliveries(orgId, subId)` (server action or route segment — choose the pattern consistent with how the activity drawer fetches detail). Render a table: delivery id (short), event_type, status, attempt, next_attempt_at (show `—` when null/zero), response_status.
- [ ] **Step 2:** Each non-succeeded row gets a **Redeliver** button → `redeliverAction(subId, deliveryId)`; on success refresh the list (router refresh / revalidate).
- [ ] **Step 3: Verify** `pnpm build`; manually redeliver a dead/failed row and confirm it flips to pending.
- [ ] **Step 4: Commit checkpoint (user).** `feat(console): webhook deliveries view + redeliver`

---

### Task 10: Per-address inbound settings on EmailIdentityPanel

**Files:**

- Modify: `app/console/settings/EmailIdentityPanel.tsx`, `app/console/settings/actions.ts`
- Test: `app/console/settings/__tests__/inbound-actions.test.ts`

- [ ] **Step 1: Read** `EmailIdentityPanel.tsx` (`HasIdentityState` has the `<form onSubmit>` editing prefixes via `./actions`) and `settings/actions.ts` for the existing prefix-edit action pattern.

- [ ] **Step 2: Write the failing test** — a `patchInboundSettingsAction` that PATCHes `/email-domains/{id}`:

```ts
import { describe, it, expect, vi } from "vitest";
vi.mock("@/lib/hail-api", () => ({ callHailApiAsOrg: vi.fn() }));
import { patchInboundSettingsAction } from "@/app/console/settings/actions";
import { callHailApiAsOrg } from "@/lib/hail-api";

it("patches inbound fields on the email domain", async () => {
  (callHailApiAsOrg as any).mockResolvedValue(
    new Response("{}", { status: 200 }),
  );
  await patchInboundSettingsAction("dom1", {
    inboundEnabled: true,
    forwardTo: ["ops@acme.com"],
    webhookUrl: "https://h",
  });
  const [path, init] = (callHailApiAsOrg as any).mock.calls[0];
  expect(path).toBe("/email-domains/dom1");
  expect(init.method).toBe("PATCH");
  expect(init.body).toMatchObject({
    inbound_enabled: true,
    forward_to: ["ops@acme.com"],
    webhook_url: "https://h",
  });
});
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Implement** `patchInboundSettingsAction(domainId, { inboundEnabled?, forwardTo?, webhookUrl?, forwardRatePerHour? })` mapping camelCase → the API's snake_case body and `callHailApiAsOrg("/email-domains/"+id, { method: "PATCH", body })`; return the parsed response (which includes `webhook_secret` once when `webhook_url` is set). Add `rotateDomainWebhookSecretAction(domainId)` → `POST /email-domains/{id}/rotate-webhook-secret`.

- [ ] **Step 5: UI** — add an inbound section to `HasIdentityState` matching its existing `lbl`/input markup: a toggle for `inbound_enabled`, a `forward_to` list editor (chips or comma field), a `webhook_url` field + "rotate secret" button showing the secret once. On save, call the action; surface the API's 422 messages inline (the panel already has a `status` message slot).

- [ ] **Step 6: Run** the action test + `pnpm build` → green; manually toggle inbound + set a forward and confirm via `GET /email-domains`.

- [ ] **Step 7: Commit checkpoint (user).** `feat(console): inbound forwarding + webhook settings per address`

---

### Task 11: Suppressed-reason surfacing

**Files:**

- Modify: `lib/activity-queries.ts` (detail), `app/console/activity/ActivityDrawer.tsx`
- Test: extend `lib/__tests__/email-detail.test.ts`

- [ ] **Step 1: Decide the source.** A suppressed forward emits an `email.received.suppressed` webhook event but does not change the email row. Two viable sources: (a) the inbound row's `metadata` if ingest stamps a suppression reason there, or (b) the most recent `webhook_deliveries`/event for that email with `event_type='email.received.suppressed'`. **Read** how suppression is recorded — check `core/hailhq/core/email_ingest.py` (does it persist the reason on the Email row's `metadata`?) and the `emails` schema. If the reason is NOT persisted on the row, add a tiny ingest change to stamp `metadata.suppressed_reasons` (coordinate with the billing plan, which already touches ingest) — note this as a cross-repo dependency. Prefer (a): a row-local field is simpler for the console to read than reconstructing from events.

- [ ] **Step 2: Write the failing test** — `getEmailDetail` exposes `suppressedReasons: string[]` from the row metadata; assert an inbound row with `metadata.suppressed_reasons = ["insufficient_funds"]` maps to `d.suppressedReasons`.

- [ ] **Step 3: Implement** — select `metadata` in `getEmailDetail`, map `suppressedReasons`. In the drawer, when present, render a quiet line e.g. "Forwarding skipped: out of credit" (map reasons → human text: `insufficient_funds`→"out of credit", `forward_rate_limit`→"forward rate limit", `forward_loop`→"forwarding loop", `inbound_rate_limit`→"inbound rate limit"), linking `insufficient_funds` to `/console/billing`.

- [ ] **Step 4: Run** → PASS; `pnpm build`.

- [ ] **Step 5: Commit checkpoint (user).** `feat(console): show why a forward was suppressed`

---

### Task 12: Overview roadmap flip + verification sweep

**Files:**

- Modify: `app/console/page.tsx`
- Test: `pnpm vitest run`, `pnpm tsc --noEmit`, `pnpm build`

- [ ] **Step 1:** In `app/console/page.tsx`, change the "inbound email" roadmap item from "next"/pending to shipped (match the existing roadmap item markup).
- [ ] **Step 2:** `pnpm vitest run && pnpm tsc --noEmit && pnpm build` → all clean.
- [ ] **Step 3:** Manual end-to-end: receive a mail → it appears in Activity with the email dot; open drawer → body, attachments, Advanced (verdicts/raw collapsed), thread strip; set forwarding + webhook from Settings; create an org-wide subscription on /console/webhooks, see its delivery, redeliver one; confirm the secret is shown once only.
- [ ] **Step 4: Commit checkpoint (user).** `feat(console): inbound email self-serve console end-to-end`.

---

## Self-review

- **Spec coverage:** drawer extension (verdicts behind Advanced, attachments, thread) → Tasks 2–5, 11; webhooks section → Tasks 6–9; per-address settings → Task 10; suppressed reasons → Task 11; overview → Task 12; console→API auth via existing UI-key path → Task 1 (spec's JWT idea consciously dropped — documented above). Reads-direct-from-DB preserved (Tasks 2,5,6).
- **Type consistency:** `callHailApiAsOrg(path, { method, body?, redirect? })` used identically in Tasks 1,3,7,10. `getEmailDetail` returns the extended `EmailDetail` (Task 2) consumed by Tasks 4,5,11. Action bodies map camelCase→snake_case at the action boundary only.
- **Placeholder scan:** UI/JSX tasks say "match existing markup in this file" with the exact data contract + field list rather than fabricating classNames not yet read — deliberate, since the components must stay visually consistent. Data/query/action layers carry full code.
- **Cross-plan dependency:** Task 11 may require ingest to stamp `metadata.suppressed_reasons`; the billing plan already edits `email_ingest.py`. Sequence: do billing first, then this Task 11 builds on a stamped field (or reads events). Flagged.
