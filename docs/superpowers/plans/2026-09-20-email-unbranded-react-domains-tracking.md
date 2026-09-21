# Unbranded email, react-email docs, guided domains, tracking fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emails leave hail with no hail footer, full-document react-email HTML is sent unchanged and documented, custom-domain setup names the DNS host and shows which records hail can see, and the console tracking screens show correct data.

**Architecture:** `hail` deletes the footer module and adds one read-only route `GET /v1/email-domains/{id}/dns-check` built on the existing DNS-over-HTTPS helper. `hail-website` reads that route for pending domains and fixes display bugs. No new dependencies in either repo.

**Tech Stack:** FastAPI, Pydantic v2, httpx, pytest; Go + oapi-codegen (CLI); Next.js 16, React 19, vitest.

**Spec:** `docs/superpowers/specs/2026-09-19-email-unbranded-react-domains-tracking-design.md` (repo `hail`). Executors read it first; it holds every file:line.

## Global Constraints

- The user chose lean plans: each task names files, interfaces, tests and commands. The worker writes the test first (TDD), then the code.
- `hail` work happens only in `/Users/r/playground/hail/.claude/worktrees/email-unbranded-domains-guided` (branch `feat/email-unbranded-domains-guided`).
- `hail-website` work happens only in `/Users/r/playground/hail-website/.claude/worktrees/console-email-domains-tracking` (branch `feat/console-email-domains-tracking`, base `origin/master`).
- Commit after each task. Conventional Commits. **No `Co-Authored-By` or any AI trailer.** No push, no PR, no merge, no tag — the controller does those.
- Python env: run `uv sync --all-packages --all-extras` at the repo root only. Never `uv sync` inside a sub-package.
- No new dependencies. No new env vars. No scope beyond the spec.
- `hail`: URLs via `hailhq.core.urls` helpers. Providers only through `core`.
- `hail-website`: read `AGENTS.md` first; Next.js docs are in `node_modules/next/dist/docs/`; URLs via `lib/urls.ts`; console pages stay noindex.
- After regenerating `openapi/openapi.yaml`, run `pnpm exec prettier --write openapi/openapi.yaml`. If `pnpm install` dirties `pnpm-lock.yaml`, `git checkout pnpm-lock.yaml`.

---

## Repo `hail`

### Task 1: Delete the footer; prove full-document HTML is sent unchanged

**Files:**
- Delete: `core/hailhq/core/email_footer.py`, `core/tests/test_email_footer.py`
- Modify: `api/hailhq/api/routes/emails.py` (import, docstring ~:300, call ~:315 — pass `email.body_text` / `email.body_html` to the provider), `core/hailhq/core/email_forwarding.py:24,108`, `core/tests/test_email_forwarding.py:207`
- Test: `core/tests/providers/test_ses_email.py`, the API test file that covers `POST /emails` wire content (find with `grep -rn "Sent via\|append_sent_footer\|Hail.so" api/tests core/tests`)

- [ ] Write failing tests: (a) forward body contains no `Hail.so`; (b) `POST /v1/emails` with `body_html = "<!DOCTYPE html><html><head></head><body><p>x</p></body></html>"` → fake provider receives the identical string and identical `body_text`; (c) `SesEmailProvider.send_email` passes that string byte-identical in both `Simple` and `Raw` (attachment) paths.
- [ ] Run them, see them fail. Remove the footer. Invert every old footer assertion.
- [ ] `grep -rn "email_footer\|append_sent_footer\|append_forwarded_footer\|Sent via Hail\|Forwarded by Hail" --include="*.py" --include="*.md" --include="*.mdx" . | grep -v docs/superpowers` returns nothing. Fix docs hits (README, `docs/public/**`, MCP tool descriptions, SDK docstrings).
- [ ] `uv run pytest core api -q`, `uv run ruff check .`, `uv run black --check .`, `uv run mypy` (as CI runs it). Commit `feat(email)!: remove hail footer from sent and forwarded mail`.

### Task 2: react-email docs page

**Files:**
- Create: `docs/public/react-email.md`
- Modify: `docs/public/meta.json` (insert `"react-email"` after `"webhooks"`), `docs/public/README.md` if it lists pages

- [ ] Page content, one screen: install `@react-email/components` + `@react-email/render`; a 10-line `Welcome.tsx`; a Node `fetch` to `POST {HAIL_API_URL}/v1/emails` with `Authorization: Bearer $HAIL_API_KEY` and body fields `to`, `from`, `subject`, `body_html: await render(<Welcome />)`, `body_text: await render(<Welcome />, { plainText: true })`, `recipient_consent: true`; a Python SDK variant that reads a pre-rendered `.html` file. Confirm field names and auth header against `openapi/openapi.yaml`, not memory.
- [ ] Notes block: `html` / `text` field names return 422; an HTML body is needed for open and click events; hail adds nothing to the body; the sender discloses AI use where the law asks for it.
- [ ] Check how `docs-site` builds `llms.txt` (`docs-site/app/llms.txt`, `docs-site/lib/source.ts`) and confirm the page is included; `cd docs-site && pnpm build` passes. Commit `docs: send react-email templates through hail`.

### Task 3: DNS helpers in core

**Files:**
- Modify: `core/hailhq/core/dns_lookup.py`
- Test: `core/tests/test_dns_lookup.py` (create or extend; mock httpx the way existing `resolve_mx` tests do)

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class DnsProvider: id: str; name: str; dns_url: str; note: str | None
async def _resolve(name: str, rtype: str) -> list[str]   # raw `data` strings, [] on any httpx/JSON error
async def resolve_mx(domain: str) -> list[str]            # unchanged behaviour, rebuilt on _resolve
async def resolve_zone_ns(domain: str) -> tuple[str, list[str]]  # strips left labels until NS answers; ("", []) if none
def detect_dns_provider(nameservers: list[str]) -> DnsProvider | None
async def observe_record(record: dict) -> bool            # keys: type, name, value
async def dmarc_present(zone: str) -> bool                # TXT at _dmarc.<zone> starting "v=DMARC1"
```
- [ ] Tests first: zone walk (`inbox.mail.example.com` → `example.com`), each provider suffix from the spec table, unknown NS → `None`, CNAME/MX/TXT observe with trailing dots, quotes and case differences, DoH error → `False` / `[]`, `resolve_mx` keeps raising nothing new for callers (check current callers' expectations before changing its error behaviour; keep it as is if they rely on the raise).
- [ ] Provider `dns_url`: verify each against the host's own docs with WebFetch. If it cannot be verified, use the host's login page. Cloudflare `note`: "Set Proxy status to DNS only for every CNAME."
- [ ] Lint + tests green. Commit `feat(core): dns host detection and record observation`.

### Task 4: Route `GET /v1/email-domains/{id}/dns-check`

**Files:**
- Modify: `core/hailhq/core/schemas.py` (after `EmailDomainResponse`), `api/hailhq/api/routes/email_domains.py` (register above `GET /{domain_id}` only if path matching needs it), `openapi/openapi.yaml`, `cli/internal/client/client.gen.go`
- Test: `api/tests/test_email_domains_dns_check.py`

**Interfaces — Produces** (every field carries a `description=`; 0.22.0 made that a rule):
```python
class DnsProviderSchema(BaseModel): id: str; name: str; dns_url: str; note: str | None
class ObservedDnsRecord(DnsRecordSchema): observed: bool
class DmarcCheck(BaseModel): present: bool; suggested: DnsRecordSchema | None
class EmailDomainDnsCheck(BaseModel):
    dns_provider: DnsProviderSchema | None; zone: str | None
    records: list[ObservedDnsRecord]; dmarc: DmarcCheck
```
- [ ] Tests first (monkeypatch the Task 3 functions): happy path; other org → 404; `hail_mail` row → `records: []`, `dns_provider: null`, `dmarc.suggested: null`; DMARC present → `suggested: null`; absent → `{"type":"TXT","name":"_dmarc.<zone>","value":"v=DMARC1; p=none;"}`.
- [ ] Implement with `asyncio.gather`. Same auth, rate-limit and legacy-path treatment as the sibling `GET /{domain_id}` route.
- [ ] Regenerate: command at `docs/public/self-host/operations.md:76`, then prettier, then `cd cli && make generate` (read `cli/Makefile` for the real target). `git diff --stat openapi/` must be small.
- [ ] Full checks. Commit `feat(api): email domain dns-check route`.

### Task 5: SDK + CLI

**Files:**
- Modify: `sdk/hail/client.py` (domain class near :509, add `async def dns_check(self, domain_id) -> EmailDomainDnsCheck` in the file's existing style; sync client too if one exists), SDK models/exports, `cli/internal/cmd/email_domain.go` (new `dns-check <id>` subcommand, table output: TYPE NAME VALUE SEEN, plus host line and DMARC line; `--json` as siblings do), `docs/public/cli.md`
- Test: SDK test next to the existing domain tests; `cli/internal/cmd/email_domain_test.go` in the existing style

- [ ] Tests first, then code. `uv run pytest sdk -q`, `cd cli && go test ./... && gofmt -l .` empty. Do not bump versions here. Commit `feat(sdk,cli): email domain dns-check`.

---

## Repo `hail-website`

### Task 6: Guided domain flow

**Files:** `lib/` (new query beside `getCustomDomains`; find with `grep -rn "getCustomDomains" lib app`), `app/console/domains/actions.ts`, `page.tsx`, `CustomDomainsPanel.tsx`, `DomainRow.tsx`, `DnsRecordsBlock.tsx`, `lib/dns-records.ts`; tests in `app/console/domains/__tests__/`.

**Interfaces — Consumes:** `GET /email-domains/{id}/dns-check` → the Task 4 JSON shape (spec A3). The API may not be deployed yet: on 404 or error the query returns `null` and the UI renders exactly as today.

- [ ] Tests first for pure logic (match observed flags to records by `type+name+value`, DMARC group, null fallback).
- [ ] Fetch the check server-side for each pending domain in `page.tsx`, so the existing 10 s `router.refresh()` updates it.
- [ ] UI: host line "Your DNS is hosted at {name}." + link "Open DNS settings" (`target="_blank" rel="noopener noreferrer"`), host note, per-record tag "found" / "not found yet", group "DMARC — recommended" with the suggested record and copy buttons. Reuse existing tag and copy-button components. CSV / BIND export includes the DMARC record.
- [ ] `pnpm test && pnpm lint`. Commit `feat(console): name the dns host and show which records are found`.

### Task 7: Tracking display fixes

**Files:** `app/console/email/page.tsx:27`, `lib/activity-queries.ts:310-334`, `app/console/email/DeliverabilityDashboard.tsx`, `lib/deliverability.ts:108-111`, `app/console/email/StatsChart.tsx:110-119`; tests in `lib/__tests__/`.

- [ ] Tests first: `getRecentProblemEmails(orgId, window)` filters by window; `eventTagVariant` returns the danger variant for bounced / complained / rejected; stacked bar total never exceeds `sent` when `complained` emails are also in `delivered`.
- [ ] Chart: backend `_STATS_SQL` counts events per kind, and an SES complaint always follows a delivery, so use `other = max(0, sent − delivered − bounced)` and draw complaints as a part of the delivered bar, not on top of it.
- [ ] "Recent problems": obey the window, footnote "Showing the 10 most recent.", whole row is a link to `/console/activity?channel=email&…` opening that email (copy the keyboard pattern from `ActivityClient.tsx:196-210`).
- [ ] Text "Dates and times are UTC." beside the range controls. Legend no longer `aria-hidden`.
- [ ] Tests + lint. Commit `fix(console): email health data matches the selected range and event kinds`.

### Task 8: Accessibility, tabs, loading and error states

**Files:** `MailOnboardingWizard.tsx:67-116`, `EmailIdentityPanel.tsx:62-157`, `app/console/settings/actions.ts:237,253`, `DnsRecordsBlock.tsx:83-90`, `lib/channel-tabs.ts:19-21` (+ its test), `console.css:2131`, new `app/console/loading.tsx`, `app/console/error.tsx`.

- [ ] `<label htmlFor>` on every wizard and identity input; replace the `✅` with the existing tag component; `baseDomain` prop instead of literal `mail.hail.so`; `revalidatePath("/console/domains")`.
- [ ] Export menu: ArrowUp/ArrowDown move focus, Home/End, Escape closes and returns focus to the button, `aria-expanded` on the button.
- [ ] `EMAIL_TABS` gets `Activity` → `/console/activity?channel=email`; check how the active tab is computed so Activity is not shown active on other channels.
- [ ] `loading.tsx`: skeleton from existing console classes. `error.tsx`: client component, message + "Try again" calling `reset()`. Read the Next 16 docs in `node_modules/next/dist/docs/` for both file conventions.
- [ ] `pnpm test && pnpm lint && pnpm build`. Commit `fix(console): labels, keyboard menu, email activity tab, loading and error states`.

### Task 9: Text that promises the footer

- [ ] `grep -rn -i "sent via\|footer\|disclos\|forwarded by" content app/llms.txt app/skill.md lib/agent-skill-doc.ts "app/(marketing)" docs/seo 2>/dev/null`. Change only sentences that say hail adds a line to customer email. Legal pages: list each hit with file:line in the task report; change wording only where it states the footer as fact. Commit `docs: hail no longer adds a footer to customer email`.

---

## After the tasks (controller)

1. Push both branches, open both PRs.
2. User merges `hail` PR → deploy → live send 2 → read events → confirm no footer.
3. Release PR 0.23.0 per spec A4.
