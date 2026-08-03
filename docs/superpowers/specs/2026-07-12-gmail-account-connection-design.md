# Gmail Account Connection — Design

**Date:** 2026-07-12
**Status:** Approved design, pending implementation plan

## Summary

Let an org connect Gmail accounts to Hail so agents can send email as the user's
real address and read the user's inbox live. Complements (does not replace)
hail-mail and custom-domain sending via SES.

## Decisions

| Topic          | Decision                                                                              |
| -------------- | ------------------------------------------------------------------------------------- |
| Providers (v1) | Gmail only; Outlook later once the pattern is proven                                  |
| Ownership      | Multiple connected accounts per org                                                   |
| Mailbox role   | Real Gmail threads: sends land in the user's Sent folder; replies live in their Gmail |
| Integration    | Direct Gmail REST API via async httpx; no Google SDK, no Nango                        |
| Receive model  | **Ephemeral only** — live pass-through reads, nothing received is ever stored in Hail |
| Read scope     | Full inbox with Gmail query syntax (capability of the agent, not stored data)         |
| Connect flow   | API-hosted OAuth; console/CLI/MCP just open the returned URL                          |
| Google app     | Already verified/production — no testing-mode or CASA caveats                         |
| Billing        | Gmail sends bill at the standard email rate (0.2¢), same ledger path as SES sends     |
| Aliases        | Primary address only in v1; Gmail send-as aliases later                               |
| Webhooks       | None for Gmail inbound (nothing is ingested)                                          |

## 1. Data model

New table `email_accounts` (Alembic migration 0029):

| Column                      | Notes                                                                |
| --------------------------- | -------------------------------------------------------------------- |
| `id`                        | UUID PK                                                              |
| `organization_id`           | FK → organizations                                                   |
| `provider`                  | `'gmail'` (CHECK constraint; room for `'outlook'`)                   |
| `email_address`             | globally unique                                                      |
| `display_name`              | from Google profile                                                  |
| `provider_user_id`          | OIDC `sub` (provider-neutral name); detects wrong-account reconnects |
| `scopes`                    | granted scopes as stored text/JSON                                   |
| `encrypted_refresh_token`   | Fernet via `hailhq.core.secret_cipher` (`HAIL_PROVIDER_SECRET_KEY`)  |
| `status`                    | `active \| reauth_required \| disabled`                              |
| `created_at` / `updated_at` | timestamps                                                           |

No sync cursor columns (`history_id`, `last_synced_at`) — receive is ephemeral.

Changes to `emails`:

- `email_account_id` — nullable FK (RESTRICT), mirrors `email_domain_id`;
  exactly one of the two is set per row.
- `provider_thread_id` — Gmail threadId, set on Gmail-sent rows.

`email_domains` is untouched. OAuth secrets live only in `email_accounts`,
which no existing code path reads or serializes.

## 2. Connect flow (OAuth)

API-hosted; every surface (console, CLI, MCP) just opens a URL.

- `POST /email-accounts/connect` → `{authorization_url}`.
  Scopes: `gmail.send`, `gmail.readonly`, `openid`, `email`.
  `access_type=offline&prompt=consent` so a refresh token is always issued.
  `state` = short-TTL signed token binding org + user.
- `GET /email-accounts/oauth/callback` — exchanges the code, calls Gmail
  `getProfile` for the address, upserts the `email_accounts` row, then
  redirects to the console (env-configured URL) or renders a minimal success
  page for headless users.
- `GET /email-accounts` / `GET /email-accounts/{id}` — list/read (never
  return token material).
- `PATCH /email-accounts/{id}` — `{status: "active" | "disabled"}`;
  `reauth_required` is server-managed and cannot be set or cleared here.
- `DELETE /email-accounts/{id}` — revoke token at Google, then delete.
  Blocked (409) while `emails` rows reference the account (RESTRICT FK);
  disable via `PATCH {status: "disabled"}` instead.
- `POST /email-accounts/{id}/reconnect` — same as connect for an existing
  row; `google_user_id` mismatch on callback → 409 (wrong Google account).

## 3. Send path

- **Sender resolution** (`_resolve_sender`, `api/hailhq/api/routes/emails.py`):
  explicit `from` matching an _active_ `email_accounts` row → Gmail path.
  All existing behavior (explicit domain match, default-domain fallback,
  hail-mail auto-mint) is unchanged; no implicit behavior change.
- **`GmailEmailProvider`** at `core/hailhq/core/providers/email/gmail.py`
  (provider-adapter invariant: SDK/HTTP calls only in core providers):
  - Protocol split: extract a narrow `EmailSender` base (only `send_email`)
    from `EmailProvider`; `SesEmailProvider` keeps the identity-management
    methods on top; Gmail implements `EmailSender` only.
  - Builds RFC 2822 MIME with the same builder as SES raw sends
    (attachments, cc/bcc identical), calls `users.messages.send`
    (`uploadType=multipart` for large attachments) via async httpx.
  - Token refresh = one POST to Google's token endpoint; access token cached
    in memory with expiry; refresh failure → account `reauth_required`.
  - Replies: if `in_reply_to` (RFC822 Message-ID) is supplied, resolve the
    Gmail thread via `rfc822msgid:` search; send with `threadId` +
    `In-Reply-To`/`References` headers so threading holds in both mailboxes.
- **`emails` row**: `provider='gmail'`, `provider_message_id` (Gmail id),
  `provider_thread_id`, `status='sent'`, `email_account_id` set.
- **Billing**: same ledger write as SES sends at the standard email rate.
  No `private-rates.ts` change.
- Gmail has no SES-style event stream: `/emails/{id}/events` stays empty for
  Gmail sends; status terminates at `sent`. Bounces appear as mailer-daemon
  replies in the Gmail thread, visible via live reads. Gmail's own send
  quotas surface as provider errors; Hail does not pre-enforce them.

## 4. Live reads (ephemeral, nothing stored)

- `GET /email-accounts/{id}/messages?q=<gmail query>&max_results=N`
  — proxies Gmail search/list; returns message metadata + snippets.
- `GET /email-accounts/{id}/messages/{message_id}` — full message: body,
  headers (including RFC822 `Message-ID` for replying), attachment list.
- Nothing read is persisted — no `emails` rows, no S3, no webhook events.
- **MCP tools**: `list_email_accounts`, `search_mailbox`,
  `read_mailbox_message`. "Check my last emails" → `search_mailbox` with
  `in:inbox`, then read on demand. Names are provider-neutral for Outlook
  later.
- Reply loop: read returns `Message-ID` → agent passes it as `in_reply_to`
  to `POST /emails` → §3 threading.

## 5. Error handling

- Refresh token revoked/expired → `status='reauth_required'`; sends and
  reads on that account return 409 with a reconnect hint.
- Google 429/5xx → reads surface as provider errors; failed sends mark the
  `emails` row `failed` with the Google error detail.
- Config gating at startup: connect endpoints return 503 unless
  `GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` and
  `HAIL_PROVIDER_SECRET_KEY` are set (matches existing config-gating
  patterns).

## 6. Testing

- Unit: `GmailEmailProvider` against mocked Gmail REST — send, token
  refresh, `rfc822msgid:` thread resolution, MIME parity with the SES
  builder.
- Routes: connect + callback with mocked token exchange (state validation,
  wrong-account reconnect), messages proxy, sender resolution choosing
  account vs domain.
- Optional env-gated live smoke test against a real test Gmail account.

## 7. Config & repo chores (same PR)

- `.env.example`: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`.
- Regenerate `openapi/openapi.yaml`; Go CLI picks up the new endpoints.
- Register new MCP tools in `mcp/hailhq/mcp/tools.py`.
- Alembic migration 0029.

## Out of scope (v1)

- Outlook / Microsoft Graph (same seams: `provider='outlook'`, new adapter).
- Persisted inbound (sync cursor machinery) and real-time push
  (`users.watch` + Pub/Sub) — both were designed and deliberately deferred.
- Gmail send-as aliases.
- Polling workers and Gmail webhook events.
