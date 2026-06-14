# Inbound email — docs design

Status: approved
Owners: r13i
Repos: `hail/` (docs/), `hail-website/` (docs registry)

## Goal

Cover the two tenant-facing gaps left after the inbound-email feature shipped:
(1) how to consume Hail webhooks, and (2) the new CLI commands. Operator setup
docs (`aws-ses.md` §10) are already current — verified.

## Decisions (locked)

1. **No pricing page** — Hail has no public per-channel rate card; rates live in
   `hail-website/lib/private-rates.ts` and tier copy only. The billing spec
   handles the inbound rate there. Nothing to do here.
2. **Operator setup is done** — `docs/setup/aws-ses.md` §10 (MX, receipt rule,
   env vars, rule-set activation, `HAIL_WEBHOOK_SECRET_KEY`) and `.env.example`
   are current. **But `aws-ses.md` is not registered in the website docs nav** —
   fix that (item C).
3. Docs are agent-first per repo tenets: lead with a runnable example, link
   canonical sources (OpenAPI spec, code paths), one screen where possible.

## Changes

### A. Webhook consumer guide (new) — `hail/docs/setup/webhooks.md`

Tenant-facing "you configured a webhook, here's how to receive it":

- **Lead with a runnable verify snippet** (Python + Node): recompute
  `HMAC-SHA256` over `f"{t}.{raw_body}"` using the once-shown secret, compare to
  the `v1=` value in `X-Hail-Signature: t=<unix>,v1=<hex>`, constant-time.
- **Headers** per delivery: `X-Hail-Signature`, `X-Hail-Event`, `X-Hail-Delivery`,
  `X-Hail-Subscription` / `X-Hail-Email-Domain` (when applicable).
- **Event types**: `email.received`; `email.received.suppressed` with
  `data.reason ∈ {forward_loop, forward_rate_limit, inbound_rate_limit,
insufficient_funds}`; note `email.bounced` / `email.complained` are
  subscribable but only fire once SES bounce/complaint ingestion lands (next
  milestone).
- **Payload shape**: link the canonical source rather than paraphrasing; show one
  example `email.received` body (id, from/to, subject, verdicts, attachment URLs,
  raw URL).
- **Retry behavior**: ladder 0s/30s/2m/10m/1h/6h/24h, dead after 7, subscription
  auto-disables after 50 consecutive dead; redeliver via console or
  `hail webhooks redeliver`.
- **Two ways to configure**: per-address (`PATCH /email-domains/{id}` webhook_url)
  vs org-wide subscription (`POST /webhooks`); both deliver the same signed
  payload.

### B. CLI reference (new) — `hail/docs/cli.md`

No CLI reference doc exists today (only `--help` + scattered README mentions).
Add a concise command reference covering the email/webhook surface, leading with
runnable examples:

- `hail email list --direction inbound`
- `hail webhooks create --url … --events email.received,…`
- `hail webhooks list | deliveries <sub> | redeliver <sub> <delivery>`
- Note the `sender-domain → email domain` command rename (old name removed).

Keep it to the new/changed surface; don't document the entire CLI exhaustively —
link `--help` and the OpenAPI spec as canonical.

### C. Docs registry — `hail-website/lib/docs.ts`

Add entries so the docs site actually surfaces these:

- `setup-aws-ses` → `setup/aws-ses.md`, "Email & inbound setup" — **currently
  missing; the whole email setup doc is invisible on the site without it.**
- `setup-webhooks` → `setup/webhooks.md`, "Webhooks".
- `cli` → `cli.md`, "CLI reference".

(`app/llms.txt/route.ts` reads the same registry, so the manifest updates for
free.)

## Testing / verification

- Webhook verify snippet is **copy-paste runnable**: include a self-contained
  example with a known secret + body + signature that the snippet validates to
  `true` (so a reader can confirm their implementation against it).
- Docs registry: each new slug resolves to an existing file path in the `hail`
  repo at the pinned blob base (broken-link check); `aws-ses.md` now appears in
  the docs nav.
- Lint prose with the repo's markdown conventions; one-screen check.

## Risks / notes

- The webhook payload shape is the contract — **link the OpenAPI/code source as
  canonical** and keep the doc's example illustrative, so the doc can't drift into
  a second source of truth.
- CLI reference risks staleness; keep it thin and defer detail to `--help`.

## Out of scope

- Pricing/marketing pages (none exist; not this feature's job).
- SMTP-inbound consumer docs (the provider is still stubbed).
- Rewriting operator setup (already current).
