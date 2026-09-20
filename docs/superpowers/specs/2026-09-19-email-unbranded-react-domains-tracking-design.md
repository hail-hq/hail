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
  "dns_provider": {"id": "cloudflare", "name": "Cloudflare", "dns_url": "https://dash.cloudflare.com/?to=/:account/:zone/dns/records", "note": "Set Proxy status to DNS only for every CNAME."},
  "zone": "wdstck.co",
  "records": [{"type": "CNAME", "name": "…", "value": "…", "priority": null, "observed": true}],
  "dmarc": {"present": false, "suggested": {"type": "TXT", "name": "_dmarc.wdstck.co", "value": "v=DMARC1; p=none;", "priority": null}}
}
```

- `dns_provider` is `null` when the nameservers match no known host.
- `observed` means "hail saw this record in public DNS". SES stays the authority for `verification_status`. Field description says so.
- `dmarc.suggested` is `null` when `present` is true. Suggestion is `p=none` only, no `rua`.
- 404 for another org's domain; `hail_mail` rows return empty `records`, `dns_provider: null`.

Code, all in `core/hailhq/core/dns_lookup.py` (DNS-over-HTTPS, no new dependency):

- `_resolve(name, rtype) -> list[str]`: shared DoH call; `resolve_mx` is rebuilt on it.
- `resolve_zone_ns(domain) -> tuple[str, list[str]]`: query NS on the name, strip the left label until an answer comes back. Returns the zone and its nameservers.
- `detect_dns_provider(nameservers) -> DnsProvider | None`: suffix table — `ns.cloudflare.com`, `domaincontrol.com` (GoDaddy), `registrar-servers.com` (Namecheap), `awsdns-` (Route 53), `googledomains.com` / `google.com` (Google), `squarespacedns.com`, `vercel-dns.com`, `digitalocean.com`, `ui-dns.` (IONOS), `hover.com`, `name.com`, `porkbun.com`, `gandi.net`, `ovh.net`. Each `dns_url` is checked against the host's own docs before it is committed; an unverified host gets its login page.
- `observe_record(record) -> bool`: CNAME → target equals value; MX → host in answers; TXT → value in answers. Trailing dots and case ignored.
- Lookups run with `asyncio.gather`; a DoH failure gives `observed: false`, never a 5xx.

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
