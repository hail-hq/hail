# Connect a Gmail account

Send as your real address and read your inbox live — no DNS setup, no
SES domain verification.

```bash
# 1. Get a consent URL and open it in a browser
curl -X POST $HAIL_API_URL/email-accounts/connect \
  -H "Authorization: Bearer $HAIL_API_KEY"
# → {"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}

# 2. After consenting, send as yourself
curl -X POST $HAIL_API_URL/emails \
  -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
  -d '{"from": "you@gmail.com", "to": ["bob@example.com"],
       "subject": "hi", "body_text": "hello", "recipient_consent": true}'

# 3. Check your inbox (live read — Hail stores nothing)
curl "$HAIL_API_URL/email-accounts/{id}/messages?q=in:inbox" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

Replies: pass a message's `message_id` (an RFC 2822 Message-ID, from the
`messages`/`messages/{id}` response) as `in_reply_to` on `POST /emails` to
answer inside the same Gmail thread.

MCP tools: `list_email_accounts`, `search_mailbox`, `read_mailbox_message`.

## How it works

`POST /emails` resolves the `from` address in priority order: an explicit
`from` that matches a connected `email_accounts` row wins first (routes
through Gmail), then the first verified org-owned domain, then an
auto-minted hail-mail address. So once an account is connected, sending
as that address is just a normal `POST /emails` call — no separate
"send via Gmail" endpoint.

## Endpoints

- `POST /email-accounts/connect` — mint a Google consent URL for a new
  connection.
- `GET /email-accounts/oauth/callback` — Google's redirect target
  (unauthenticated by necessity; the signed `state` token carries the
  org id).
- `GET /email-accounts` — cursor-paginated list, org-scoped.
- `GET /email-accounts/{id}` — single account.
- `PATCH /email-accounts/{id}` — set `status` to `active` or `disabled`.
- `DELETE /email-accounts/{id}` — revokes the token at Google and
  deletes the row; `409` if `emails` rows still reference it (disable
  instead).
- `POST /email-accounts/{id}/reconnect` — consent URL for an existing
  row (e.g. after `reauth_required`).
- `GET /email-accounts/{id}/messages` — live Gmail search/list
  (`q`, `max_results`, `page_token` — Gmail search syntax, e.g.
  `in:inbox`, `from:alice@example.com`).
- `GET /email-accounts/{id}/messages/{message_id}` — single message,
  with body and attachment metadata.

## Notes

- Gmail sends bill at the standard email rate and appear in
  `GET /emails`; received mail is never stored — mailbox reads proxy
  live to Gmail on every call.
- No delivery/bounce events for Gmail sends (no `email_events` rows
  beyond the initial `sent`); bounces arrive as mailer-daemon replies
  in the thread instead.
- If Google rejects the stored refresh token (revoked access, expired
  grant), the account flips to `reauth_required` and sends/reads return
  `409` until you call `POST /email-accounts/{id}/reconnect`.
- Self-hosting: create a Google Cloud OAuth client (Web application
  type, redirect URI `<HAIL_API_URL>/email-accounts/oauth/callback`),
  then set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` in
  `.env`. A Workspace "internal" app needs no Google review. Also
  requires `HAIL_PROVIDER_SECRET_KEY` (Fernet key, encrypts stored
  refresh tokens) — see `.env.example`. Optionally set
  `HAIL_EMAIL_CONNECT_SUCCESS_URL` to redirect the browser somewhere
  after a successful connect instead of showing Hail's default
  confirmation page.

## Reference

- OpenAPI: [`openapi/openapi.yaml`](../../openapi/openapi.yaml) →
  `email-accounts` tag.
- Code paths:
  [`api/hailhq/api/routes/email_accounts.py`](../../api/hailhq/api/routes/email_accounts.py),
  [`core/hailhq/core/providers/email/gmail.py`](../../core/hailhq/core/providers/email/gmail.py),
  [`core/hailhq/core/providers/email/gmail_oauth.py`](../../core/hailhq/core/providers/email/gmail_oauth.py),
  [`mcp/hailhq/mcp/tools.py`](../../mcp/hailhq/mcp/tools.py).
- Design spec: [`docs/superpowers/specs/2026-07-12-gmail-account-connection-design.md`](../superpowers/specs/2026-07-12-gmail-account-connection-design.md)
