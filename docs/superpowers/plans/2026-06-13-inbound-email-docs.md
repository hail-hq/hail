# Inbound Email Docs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Git policy:** Never run git write commands. "Commit checkpoint" steps are user-facing only.

**Goal:** Close the two tenant-facing doc gaps — a webhook consumer guide (verify `X-Hail-Signature`, event types, retries) and a CLI reference — and surface the existing (currently unregistered) email setup doc on the docs site.

**Architecture:** New markdown in `hail/docs/`, registered in `hail-website/lib/docs.ts`. Docs are agent-first: lead with a runnable example, link canonical sources, one screen where possible.

**Tech Stack:** Markdown (`hail/`), TypeScript docs registry (`hail-website/`).

Spec: `docs/superpowers/specs/2026-06-13-inbound-email-docs-design.md`

The signing scheme is fixed and authoritative in `core/hailhq/core/webhooks.py`:

- `X-Hail-Signature: t=<unix_ts>,v1=<hex_hmac_sha256>`, signed over `f"{t}.{body}"` (HMAC-SHA256, secret = the once-shown webhook secret).
- Envelope (compact JSON): `{id, type, api_version, created_at, organization_id, data}`.
- Retry ladder: `[0, 30, 120, 600, 3600, 21600, 86400]` seconds; dead after the 7th; subscription auto-disables after 50 consecutive dead.

---

### Task 1: Generate a known-good signature fixture

The verify snippet in the guide must be provably correct. Generate a real signature with the production code so the doc's example validates.

**Files:** none yet (scratch command)

- [ ] **Step 1:** From the repo root, run the real signer and capture the output:

```bash
cd /Users/r/playground/hail/core && uv run python -c '
from hailhq.core.webhooks import sign_payload
body = b"{\"id\":\"evt_123\",\"type\":\"email.received\",\"data\":{\"id\":\"em_1\"}}"
print("body =", body.decode())
print("sig  =", sign_payload(body, "whsec_example", timestamp=1700000000))
'
```

- [ ] **Step 2:** Record the printed `body` and `sig` (e.g. `t=1700000000,v1=<hex>`). These exact values go verbatim into the guide's example in Task 2 so a reader can confirm their own implementation produces the same `v1`.

- [ ] **Step 3:** No commit (scratch).

---

### Task 2: Webhook consumer guide

**Files:**

- Create: `hail/docs/setup/webhooks.md`

- [ ] **Step 1: Write the guide.** Structure (lead with the runnable verify snippet):
  1. **One-paragraph intro:** Hail POSTs a signed JSON event to your URL on inbound mail; verify the signature, return 2xx, you're done.

  2. **Verify the signature (Python):** using the Task 1 fixture as the worked example:

```python
import hashlib, hmac

def verify(secret: str, signature_header: str, raw_body: bytes) -> bool:
    # signature_header looks like "t=1700000000,v1=<hex>"
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    t, v1 = parts["t"], parts["v1"]
    mac = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), v1)

# Worked example — should print True:
assert verify(
    "whsec_example",
    "<PASTE sig FROM TASK 1>",
    b'<PASTE body FROM TASK 1>',
)
```

3. **Verify (Node):** equivalent using `crypto.createHmac("sha256", secret).update(`${t}.${rawBody}`)` and `crypto.timingSafeEqual`. Stress: **use the raw request body bytes**, not a re-serialized object.

4. **Headers table:** `X-Hail-Signature`, `X-Hail-Event`, `X-Hail-Delivery`, `X-Hail-Subscription` (org-wide subs), `X-Hail-Email-Domain` (per-domain webhooks).

5. **Event types:**
   - `email.received` — a message arrived.
   - `email.received.suppressed` — received but fan-out was held back; `data.reason ∈ {forward_loop, forward_rate_limit, inbound_rate_limit, insufficient_funds}`.
   - `email.bounced`, `email.complained` — _subscribable but not yet emitted_ (land with SES bounce/complaint ingestion next milestone). State this plainly so subscribers don't wait on silent events.

6. **Payload:** show one `email.received` example body, then **link the canonical schema** (`openapi/openapi.yaml` webhook component + `core/hailhq/core/webhook_fanout.py::build_event_data`) — the doc's example is illustrative, the code is the source of truth.

7. **Retries:** ladder `0s / 30s / 2m / 10m / 1h / 6h / 24h`; dead after 7; subscription auto-disables after 50 consecutive dead deliveries; re-enable by re-activating the subscription; replay with `hail webhooks redeliver <sub> <delivery>` or the console.

8. **Two ways to receive:** per-address (`PATCH /email-domains/{id}` `webhook_url`) vs org-wide (`POST /webhooks` with `event_types`). Same signed payload either way.

- [ ] **Step 2: Verify the snippet runs True** — copy the Python `verify(...)` + the embedded fixture into a scratch file and run it; the `assert` must pass. If it fails, the fixture or snippet is wrong — fix before committing. (This is the doc's "test".)

- [ ] **Step 3: One-screen + agent-first check** — runnable example first, canonical links not paraphrase, fits roughly one screen.

- [ ] **Step 4: Commit checkpoint (user).** `docs: webhook consumer guide (signature verification, events, retries)`

---

### Task 3: CLI reference

**Files:**

- Create: `hail/docs/cli.md`

- [ ] **Step 1: Write a concise reference** covering the new/changed surface only (lead with runnable examples; defer exhaustive detail to `--help` and the OpenAPI spec):
  - Intro: `hail` codegens from `openapi/openapi.yaml`; this page covers email + webhooks; run `hail <cmd> --help` for flags.
  - `hail email list --direction inbound` (and `--direction outbound`).
  - `hail email domain ...` — note the rename from `hail sender-domain ...` (old name removed); show set-up of a hail-mail address if such subcommands exist (verify against `cli/internal/cmd/email_domain.go` before documenting exact flags — only document flags that exist).
  - `hail webhooks create --url <https> --events email.received,email.received.suppressed`
  - `hail webhooks list`
  - `hail webhooks deliveries <subscription-id>`
  - `hail webhooks redeliver <subscription-id> <delivery-id>`
  - Note: `create` prints the signing secret once.

- [ ] **Step 2: Verify every documented command + flag exists** — cross-check each against `cli/internal/cmd/webhooks.go` and `cli/internal/cmd/email_domain.go` / `email.go`. Remove anything not actually implemented. Don't document flags that don't exist.

- [ ] **Step 3: Commit checkpoint (user).** `docs: CLI reference for email + webhooks commands`

---

### Task 4: Register docs in the website + link check

**Files:**

- Modify: `hail-website/lib/docs.ts`

- [ ] **Step 1: Read** `hail-website/lib/docs.ts` — the registry array of `{ slug, path, title }` and the `GITHUB_BLOB_BASE`/`GITHUB_RAW_BASE` resolution.

- [ ] **Step 2: Add three entries** (placed sensibly among the existing `setup-*` entries):

```ts
  { slug: "setup-aws-ses", path: "setup/aws-ses.md", title: "Email & inbound setup" },
  { slug: "setup-webhooks", path: "setup/webhooks.md", title: "Webhooks" },
  { slug: "cli", path: "cli.md", title: "CLI reference" },
```

`setup-aws-ses` is the important fix — that doc already exists (with the inbound section) but was never registered, so it's currently invisible on the docs site.

- [ ] **Step 3: Link check** — for each registry entry, confirm `path` resolves to a file that exists in the `hail` repo (the two new ones from Tasks 2–3 plus `setup/aws-ses.md`). Build the site or run the docs page so each slug renders without a fetch error; `app/llms.txt/route.ts` (same registry) should list the new titles.

- [ ] **Step 4: Verify** `cd hail-website && pnpm tsc --noEmit && pnpm build` → clean; the docs nav now shows Email & inbound setup, Webhooks, CLI reference.

- [ ] **Step 5: Commit checkpoint (user).** `docs(website): register email setup, webhooks, and CLI reference pages`

---

## Self-review

- **Spec coverage:** webhook consumer guide → Task 2; CLI reference → Task 3; register `aws-ses.md` (the invisible-doc fix) + new pages → Task 4; "no pricing page" → respected (no pricing task); operator setup already current → no task (verified in spec). All covered.
- **Accuracy guards:** the verify snippet is validated against a fixture produced by the real `sign_payload` (Tasks 1–2); CLI commands are cross-checked against the actual cobra source (Task 3 Step 2); registry paths are link-checked (Task 4 Step 3). No fabricated signatures, flags, or paths.
- **Placeholder scan:** the one intentional fill-in (`<PASTE … FROM TASK 1>`) is a generated-fixture handoff between sequential tasks, not a vague TODO — the generating command is given in full.
- **Cross-plan note:** the `insufficient_funds` reason documented in Task 2 is delivered by the billing plan; sequence docs after billing so the event reason is real.
