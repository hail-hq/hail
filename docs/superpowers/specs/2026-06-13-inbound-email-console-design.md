# Inbound email — console (dashboard) design

Status: approved
Owners: r13i
Repo: `hail-website/` (console), with thin reuse of the existing public `hail/` API

## Goal

Make inbound email fully self-serve in the console: read received mail (with
attachments + threads), configure forwarding/webhooks per address, manage
org-wide webhook subscriptions, and see why a forward was suppressed. No CLI
required.

## Decisions (locked)

1. **Extend the existing email-detail drawer** (`ActivityDrawer.tsx`). No new
   viewer. The drawer already renders from/to/timeline/body/IDs for both
   directions.
2. **Verdicts + raw MIME live behind an "Advanced" disclosure** — collapsed by
   default, de-emphasized. Attachments stay visible (they're useful). Matches the
   existing brutalist/editorial console look; no new aesthetic.
3. **The console is just another API client.** All mutations, test-sends
   (email/SMS/call), and presigned-URL fetches go through the **existing public
   API** as the org — the same surface a self-hoster uses. No new `/internal`
   endpoints, no JWT, no direct DB writes for mutations. This is the rule that
   preserves the self-hosted ↔ managed duality: the public API has zero
   console-specific code; the console consumes it like any client.
   - **Auth = a full-scope org API key**, reused per browser via an HTTP-only
     cookie, minted on first use and re-minted on 401 (the proven
     `app/actions/place-call.ts` pattern, generalized).
   - **The key is visible and revocable** in `/console/keys` like any other key
     — it has real power, so the user must see and be able to kill it. **Not**
     hidden. Rename the existing per-browser key from `UI calls · <date>` to
     `Used by Console · <date>` since it now covers more than calls.
4. **Reads stay direct against shared Postgres** (`pool.query`), as today.
   Only mutations and presigned-URL fetches cross to the API.

## Components & changes (all `hail-website/`)

### A. Email drawer + detail query

- `lib/activity-queries.ts` — extend the email-detail SELECT (~line 390) to add:
  `direction`, `spam_verdict`, `virus_verdict`, `spf_verdict`, `dkim_verdict`,
  `dmarc_verdict`, `message_id`, `in_reply_to`, `references_ids`, `raw_s3_key`;
  and `LEFT JOIN email_attachments` → list of `{id, filename, content_type,
size_bytes}`. Extend the returned type accordingly.
- `app/console/activity/ActivityDrawer.tsx`:
  - **Attachments** block (when present): filename + size, click → download.
  - **Advanced** `<details>`/disclosure (collapsed): the five verdicts (PASS/FAIL/
    GRAY badges) + "Download raw .eml". Inbound-only; hidden for outbound.
  - **Thread grouping**: when the row has `in_reply_to`/`references_ids`, show a
    compact "thread" strip linking sibling messages (matched by shared
    `message_id`/`references` within the org). v1 can be a simple "N messages in
    this thread" list; full threaded view is a follow-on if it balloons.
- **Presigned URLs**: attachments and raw are org-auth'd API endpoints
  (`GET /emails/{id}/attachments/{aid}`, `GET /emails/{id}/raw`, both 302 → S3).
  Add a server action (`app/console/activity/actions.ts`) that calls the API as
  the org (decision 3) and returns the presigned URL (follow the 302, or read
  the `Location`). The client opens it. Never expose the API token to the client.

### B. Webhooks section (new)

- `app/console/webhooks/` — new route; add to `app/console/layout.tsx` nav under
  WORKSPACE.
- **Subscriptions**: table (URL, events, status, consecutive failures), reading
  `webhook_subscriptions` via `pool.query`. Create / edit (events, target_url) /
  disable / rotate-secret — each a server action hitting the public API
  (`POST/PATCH /webhooks`, `/webhooks/{id}/rotate-secret`) as the org. **Secret
  shown once** on create/rotate (copy button), never re-fetched.
- **Deliveries**: per-subscription drawer reading `webhook_deliveries` (status,
  attempt, next_attempt_at, response_status) with a **Redeliver** button →
  `POST /webhooks/{id}/deliveries/{did}/redeliver`.

### C. Per-address inbound settings

- `app/console/settings/EmailIdentityPanel.tsx` — add an inbound section:
  `inbound_enabled` toggle, `forward_to` (chips/list), `webhook_url` + rotate
  (secret once). Each writes via `PATCH /email-domains/{id}` /
  `POST /email-domains/{id}/rotate-webhook-secret` as the org. Mirror the panel's
  existing field/edit patterns; reuse `console.css`.

### D. Suppressed-reason surfacing

- Activity / drawer: when an inbound row was suppressed (forward not sent), show
  the reason — `forward_loop`, `forward_rate_limit`, `inbound_rate_limit`,
  `insufficient_funds`. Source: the `email.received.suppressed` events (or the
  row's stored suppression metadata — confirm shape in the plan). A short
  "Forwarding skipped: out of credit" line in the drawer is enough; link
  `insufficient_funds` to Usage & billing.

### E. Overview

- `app/console/page.tsx` — flip the "inbound email" roadmap item from "next" to
  shipped.

## Auth helper (shared)

One server-side helper that, given the session, produces an org-scoped bearer for
the API (JWT per decision 3) and a `callHailApi(path, init)` wrapper —
generalize the existing `place-call.ts` fetch helper so the drawer, webhooks, and
settings actions share it.

## Testing

- Drawer: inbound row renders attachments + collapsed Advanced (verdicts/raw);
  outbound row hides Advanced. Presign action returns a URL without leaking the
  token (assert no token in client payload).
- Webhooks: create returns secret once; list/deliveries render from DB; redeliver
  action calls the right path; secret never re-rendered on subsequent GETs.
- Settings: PATCH inbound fields round-trips; rotate shows new secret once.
- Auth helper: minted token is accepted by the API (integration check against a
  running API, or a unit test of the token-mint + a recorded API 200).

## Risks / notes

- **JWT audience wiring is the one real unknown** — de-risked by the API-key
  fallback (decision 3). Validate early.
- **Thread matching** by `references`/`in_reply_to` can be fuzzy across forwarded
  chains; keep v1 to same-org exact `message_id` membership and don't over-build.
- Reading `webhook_deliveries`/`webhook_subscriptions` directly couples the
  console to those table shapes — already the pattern for `emails`/`calls`,
  acceptable.

## Out of scope

- A standalone inbox/folder UI (activity stream + drawer is the surface).
- Custom-domain inbound onboarding (next milestone; needs per-tenant MX flow).
- Full threaded conversation view (v1 = thread membership list).
- Per-org rate/forward-cap editing in the UI.
