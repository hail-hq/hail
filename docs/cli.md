# CLI reference: email & webhooks

`hail` is the Go CLI. It codegens its client from [`openapi/openapi.yaml`](../openapi/openapi.yaml) — that spec is the canonical contract; this page covers only the email and webhooks surface. Run `hail <cmd> --help` for the full, authoritative flag list.

Global flags (any command): `--api-url`, `--api-key`, `--json`. Auth resolves `--api-key > $HAIL_API_KEY > ~/.hail/credentials.json` (run `hail login`).

## Email

```bash
# List inbound mail (cursor-paginated)
hail email list --direction inbound

# List outbound mail, only failures
hail email list --direction outbound --status failed

# Fetch one email (full UUID or 4+ char prefix)
hail email get 1a2b

# Send (subject + at least one body flag required)
hail email send --to alice@example.com --subject "Hi" --body "Hello"
```

`hail email list` flags: `--direction` (`inbound|outbound`), `--status` (`queued|sent|failed|bounced|complained|received`), `--limit` (default 50), `--cursor`, `--all` (walk every page). Alias: `hail email ls`.

`hail email get <id>` prints headers, auth verdicts (SPF/DKIM/DMARC/spam/virus), the raw-MIME URL, and attachment metadata for inbound rows.

`hail email send` flags: `--to` (repeatable / comma-separated), `--cc`, `--bcc`, `--from`, `--reply-to`, `--subject` (required), `--body`, `--body-html`, `--body-file`, `--body-html-file` (`-` reads stdin), `--idempotency-key`.

### Email domains

Identities email is sent from and received on. Two kinds: `hail_mail` (operator-managed parent, verified immediately) and `custom` (your DNS; returns DKIM CNAMEs to publish, then run `verify`).

```bash
# Register a hail-mail identity (uses server prefix defaults)
hail email domain register --kind hail_mail

# Register a custom domain (prints DKIM CNAMEs to publish)
hail email domain register --kind custom --domain acme.com

hail email domain list
hail email domain get <id>
hail email domain verify <id>   # re-poll the provider for DKIM status
hail email domain delete <id>   # also drops the SES identity for custom rows
```

`register` flags: `--kind` (`hail_mail|custom`, required), `--domain` (required for `custom`), `--local-prefix-user`, `--local-prefix-org` (for `hail_mail`), `--idempotency-key`. `list` takes `--limit` / `--cursor`. Aliases: `list`→`ls`, `delete`→`rm`.

> Renamed: `hail sender-domain ...` is now `hail email domain ...`. The old name no longer exists.

## Webhooks

Org-wide outbound subscriptions. Each fires an HMAC-signed POST per matching event, retried on a fixed ladder. See [setup/webhooks.md](setup/webhooks.md) for the payload shape and signature verification.

```bash
# Register a subscription (prints the signing secret ONCE)
hail webhooks create \
  --url https://example.com/hooks/hail \
  --events email.received,email.received.suppressed

hail webhooks list

# Delivery attempts for a subscription (full UUID or 4+ char prefix)
hail webhooks deliveries <subscription-id>

# Replay a single delivery
hail webhooks redeliver <subscription-id> <delivery-id>
```

`create` flags: `--url` (required), `--events` (comma-separated; default `email.received`). Valid event types: `email.received`, `email.received.suppressed`. The response includes the plaintext signing secret — it is shown only on create; store it then.
