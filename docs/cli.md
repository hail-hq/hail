# CLI reference

`hail` is the Go CLI. It codegens its client from [`openapi/openapi.yaml`](../openapi/openapi.yaml) — that spec is the canonical contract. This page is a brief summary of each command group. Run `hail <cmd> --help` for the full, authoritative flag list.

Global flags (any command): `--api-url`, `--api-key`, `--json`. Auth resolves `--api-key > $HAIL_API_KEY > ~/.hail/credentials.json` (run `hail login`).

## Calls

Outbound phone calls only.

```bash
# Place an outbound call (consent attestation required)
hail call +15551234567 --prompt "You are a scheduling assistant." --recipient-consent

hail call status <id>     # one call (full UUID or 4+ char prefix)
hail call list
hail call tail <id>       # follow the event stream for one call
```

`hail call` flags: `--prompt` (mode A) or `--llm-url`/`--llm-key`/`--llm-model` (mode B), `--from`, `--first-message`, `--language`, `--ai-disclosure`, `--tools`, `--idempotency-key`, plus the consent flags.

## SMS

```bash
# Send an outbound SMS (consent attestation required)
hail sms +15551234567 --body "Hello" --recipient-consent

hail sms status <id>
hail sms list --status delivered

hail sms suppressions list     # the opt-out list
hail sms sender-id get         # the org's custom sender ID
```

`hail sms` flags: `--body` (required), `--from`, `--idempotency-key`, plus the consent flags. `list` takes `--status` (`queued|sent|delivered|failed|undelivered|received`), `--to`, `--limit`, `--cursor`.

## Numbers

Dedicated phone numbers for voice and SMS.

```bash
hail numbers acquire --country US --type local
hail numbers list
hail numbers get <id>
hail numbers enable-sms <id>   # attach a Messaging Service so the number can send SMS
```

`acquire` flags: `--country`, `--type` (`local|mobile|toll_free|national`), `--idempotency-key`.

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

More email subcommands: `tail <id>`, `raw <id>`, `events <id>`, `stats`, `attachment`, `attachment-upload`. Run `hail email --help` for the list.

### Email domains

The identities that send and receive email. There are two kinds: `hail_mail` (operator-managed parent domain, verified immediately) and `custom` (your DNS). For `custom`, the register call returns DKIM CNAMEs. Publish them, then run `verify`.

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

## Contacts

The org's contact directory (members + manual contacts).

```bash
hail contacts list --q alice
hail contacts create "Alice" --phone +15551234567
hail contacts update <id> --email alice@example.com
hail contacts delete <id>
hail contacts set-phone me --phone +15551234567
hail contacts clear-phone me
```

`create` requires one of `--phone` / `--email`. `list` takes `--q`, `--limit`, `--cursor`, `--all`.

## Events

```bash
hail tail                      # stream events from across the org
hail tail --id call:1a2b       # narrow to one resource
```

`hail tail` flags: `--id`, `--kind`, `--interval`, `--from-start`, `--no-follow`.

## Webhooks

Org-wide outbound subscriptions. Each subscription fires an HMAC-signed POST for each matching event. Hail retries failed deliveries on a fixed ladder. Refer to [setup/webhooks.md](setup/webhooks.md) for the payload shape, the event-type list, and signature verification.

The CLI has no `webhooks` command group. Manage subscriptions through the HTTP API:

```bash
# Register a subscription (the response shows the signing secret ONCE — store it)
curl -X POST "$HAIL_API_URL/webhooks" \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"target_url":"https://example.com/hooks/hail","event_types":["email.received","sms.received"]}'

# List subscriptions
curl "$HAIL_API_URL/webhooks" -H "Authorization: Bearer $HAIL_API_KEY"

# Delivery attempts for one subscription
curl "$HAIL_API_URL/webhooks/<sub-id>/deliveries" -H "Authorization: Bearer $HAIL_API_KEY"

# Replay one delivery
curl -X POST "$HAIL_API_URL/webhooks/<sub-id>/deliveries/<delivery-id>/redeliver" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

Other endpoints: `PATCH /webhooks/{id}` (update URL, events, or status), `DELETE /webhooks/{id}`, and `POST /webhooks/{id}/rotate-secret`. Event types cover email, SMS, and call events — the canonical list is `WebhookEventType` in [`core/hailhq/core/schemas.py`](../core/hailhq/core/schemas.py).

## Auth and utilities

```bash
hail login             # browser auth; saves an API key to ~/.hail/credentials.json
hail auth token        # print the bare API key (for scripting)
hail auth logout       # remove the local credentials file
hail mcp endpoint      # print the MCP server's Streamable HTTP URL
hail completion zsh    # shell completion script
hail version
```
