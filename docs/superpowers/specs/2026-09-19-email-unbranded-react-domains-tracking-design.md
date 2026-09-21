# Email: no hail footer, react-email docs, guided custom domains, tracking check

Date: 2026-09-19. Release target: 0.23.0.
Repos: `hail` (branch `feat/email-unbranded-domains-guided`), `hail-website` (branch `feat/console-email-domains-tracking`, cut from `origin/master`).

## Decisions (made by the user, 2026-09-19)

1. react-email support = full-document HTML passes through unchanged + a docs page. No TS SDK.
2. Footer "Sent via Hail.so …" is deleted for every org. No setting. No replacement line.
3. "Forwarded by Hail.so" line is deleted too.
4. Hidden marks stay: `X-Hail-*` forward headers, `List-Unsubscribe` on the hail API host.
5. The footer was also the AI disclosure. It goes anyway. Senders disclose AI use themselves; docs say so.
6. Custom domains: detect the DNS host, link to it, show per-record found / not found, suggest DMARC. No Cloudflare token, no Domain Connect.
7. Tracking: live send before and after deploy, plus console bug fixes. No per-message tracking switch.
8. Console: also build the email Activity tab, export-menu keyboard handling, `loading.tsx` / `error.tsx`.

## Part A — repo `hail`

### A1. Remove the footer

- Delete `core/hailhq/core/email_footer.py` and `core/tests/test_email_footer.py`.
- `api/hailhq/api/routes/emails.py:315`: send `body_text` / `body_html` as stored. Drop the import.
- `core/hailhq/core/email_forwarding.py:24,108`: drop the import and the call.
- `core/tests/test_email_forwarding.py:207`: assert the body has no "Hail.so".
- Any API test that asserts the footer on the wire: invert it.
- Not changed: `@mail.hail.so` From address for orgs without a custom domain.

### A2. react-email

- New test in `core/tests/providers/test_ses_email.py`: a `<!DOCTYPE html>…</html>` `body_html` reaches the SESv2 call byte-identical, both `Simple` and `Raw` paths.
- New API test: `POST /v1/emails` with a full-document body → provider receives the same string.
- New page `docs/public/react-email.md`, added to `docs/public/meta.json` after `webhooks`:
  - `render(<Welcome />)` → `body_html`; `render(<Welcome />, { plainText: true })` → `body_text`.
  - `fetch` example against `POST /v1/emails` with the real field names (`EmailCreate` forbids extra fields, so `html` / `text` return 422).
  - Python SDK variant: render in Node at build time, send the string.
  - Two notes: HTML body is needed for open/click tracking; hail adds nothing to the body; the sender must disclose AI use where the law asks for it.
- Confirm the page shows in the docs-site `llms.txt`.

### A3. Guided DNS — new route `GET /v1/email-domains/{id}/dns-check`

A separate route, so existing routes keep their latency and behaviour.

Response `EmailDomainDnsCheck`:

```json
{
  "kind": "custom",
  "dns_provider": {
    "id": "cloudflare",
    "name": "Cloudflare",
    "dns_url": "https://dash.cloudflare.com/?to=/:account/:zone/dns/records",
    "note": "Set Proxy status to DNS only for every CNAME."
  },
  "zone": "wdstck.co",
  "records": [
    {
      "type": "CNAME",
      "name": "…",
      "value": "…",
      "priority": null,
      "observed": true
    }
  ],
  "dmarc": {
    "present": false,
    "suggested": {
      "type": "TXT",
      "name": "_dmarc.wdstck.co",
      "value": "v=DMARC1; p=none;",
      "priority": null
    }
  },
  "lookup_ok": true
}
```

- `dns_provider` is `null` when the nameservers match no known host.
- `kind` is the domain's kind. Clients use it to tell a `hail_mail` row from a `custom` row with no records.
- `observed` is `true` when hail saw this record in public DNS and `false` when it looked and did not. It is `null` when the lookup for that record failed: "could not check", never "not published". SES stays the authority for `verification_status`. Field description says so.
- `dmarc.present` is true when a record exists at `_dmarc.<domain>` or at `_dmarc.<organizational domain>` (RFC 7489 section 6.6.3). A delegated subzone (`mail.acme.com`) thus sees the policy at `_dmarc.acme.com`.
- `dmarc.suggested` is `null` when `present` is true. Suggestion is `p=none` only, no `rua`, named `_dmarc.<zone>`.
- `lookup_ok` is `false` when the zone or DMARC lookup failed, or the check passed its 8 s deadline. Then `dns_provider` and `zone` are `null` and each `observed` is `null`. One failed record lookup does not set it to `false`.
- 404 for another org's domain; `hail_mail` rows return empty `records`, `dns_provider: null`.

Code, all in `core/hailhq/core/dns_lookup.py` (DNS-over-HTTPS). One new dependency: `tldextract` (BSD-3-Clause) for the Public Suffix List. It uses the snapshot in the wheel: no network fetch, no cache write.

- `_resolve(name, rtype) -> list[str]`: shared DoH call; `resolve_mx` is rebuilt on it.
- `organizational_domain(domain) -> str`: one label under the public suffix (`acme.co.uk` for `mail.acme.co.uk`). Private suffixes count (`vercel.app`, `github.io`).
- `resolve_zone_ns(domain) -> tuple[str, list[str]]`: query NS on the name, strip the left label until an answer comes back. The walk stops at the organizational domain and never queries a public suffix: `co.uk` has NS records of its own. Returns the zone and its nameservers.
- `detect_dns_provider(nameservers) -> DnsProvider | None`: suffix table — `ns.cloudflare.com`, `domaincontrol.com` (GoDaddy), `registrar-servers.com` (Namecheap), `.awsdns-` (Route 53), `squarespacedns.com`, `vercel-dns.com`, `digitalocean.com`, `ui-dns.` (IONOS), `hover.com`, `name.com`, `porkbun.com`, `gandi.net`, `ovh.net`. No Google entry: legacy Google Domains and Google Cloud DNS share `ns-cloud-*.googledomains.com`, so those return `null`. Suffixes match whole labels only. Each `dns_url` is checked against the host's own docs before it is committed; an unverified host gets its login page.
- `observe_record(record) -> bool`: CNAME → target equals value; MX → host in answers; TXT → value in answers. Trailing dots and case ignored.
- Lookups run in an `asyncio.TaskGroup` on one shared `httpx.AsyncClient` (`doh_client()`). A failed zone walk cancels the record lookups still in flight. A DoH failure is never a 5xx: see `observed` and `lookup_ok` above.

Surfaces: `openapi/openapi.yaml` regenerated + `pnpm exec prettier --write`; Go client regenerated; CLI `hail email domain dns-check <id>`; SDK method `dns_check_email_domain(id)` next to the existing domain methods in `sdk/hail/client.py`. No MCP tool (not asked for).

Tests: `core/tests/test_dns_lookup.py` (zone walk, provider table, observe per type, DoH failure), `api/tests/test_email_domains_dns_check.py` (happy, other org 404, hail_mail, DMARC present / absent), SDK and CLI tests in their existing style.

### A4. Release 0.23.0

Per `docs/public/self-host/operations.md` "Cutting a repo release": CHANGELOG section, SDK bump to 0.16.0 (`sdk/pyproject.toml`, `sdk/hail/__init__.py`, `uv lock`), tags `sdk-v0.16.0` and `cli-v0.22.0`, one `git tag` call per tag, no bare `v` tag. The user merges PRs and pushes tags.

## Part B — repo `hail-website`

### B1. Domain flow

- `lib/` query + server action for `GET /email-domains/{id}/dns-check`; called for each pending domain on the existing 10 s refresh.
- `DnsRecordsBlock.tsx`: host line ("Your DNS is at Cloudflare — open DNS settings"), host note, per-record "found" / "not found yet" tag, DMARC group "Recommended" with the suggested record.
- `lib/dns-records.ts`: DMARC group copy.
- `MailOnboardingWizard.tsx:67,110`: real `<label htmlFor>`; drop the emoji.
- `EmailIdentityPanel.tsx:72,88,157`: `htmlFor`; use `baseDomain`, not literal `mail.hail.so`.
- `settings/actions.ts:237,253`: revalidate `/console/domains`.
- `DnsRecordsBlock.tsx:83-90` export menu: arrow keys, Escape, focus return.

### B2. Tracking display

- `email/page.tsx:27` + `lib/activity-queries.ts:310`: "Recent problems" takes the selected window; footnote "Showing the 10 most recent."; row links to the Activity drawer.
- `DeliverabilityDashboard.tsx`: "Dates are UTC" next to the range controls.
- `lib/deliverability.ts:108`: bounced / complained / rejected → danger variant.
- `StatsChart.tsx:110`: confirm against `_STATS_SQL` that a complained email is also counted delivered; if so `other = sent − delivered − bounced` and complaints are drawn inside the delivered bar.
- `channel-tabs.ts:19`: add Activity tab → `/console/activity?channel=email`. Fix stale comment `console.css:2131`.
- `app/console/loading.tsx` and `app/console/error.tsx`.

### B3. Legal and agent docs

Grep `content/legal/`, `app/llms.txt`, `app/skill.md`, `lib/agent-skill-doc.ts`, marketing pages for a promised footer or disclosure line; change the text to match A1.

## Order

1. `hail` PR → user merges → GitHub Actions deploys.
2. Live send 2: proves tracking and the missing footer on prod.
3. `hail-website` PR → user merges → Vercel deploys.
4. Release PR 0.23.0 in `hail` → user merges → tags.

## Checks before each PR

- `hail`: `uv run ruff check`, `uv run black --check`, `uv run mypy`, `uv run pytest` at the root with `--all-packages --all-extras` synced; `cd cli && go test ./...`.
- `hail-website`: `pnpm test`, `pnpm lint`, `pnpm build`.
