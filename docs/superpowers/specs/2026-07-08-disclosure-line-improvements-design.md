# Disclosure line improvements (email + voice)

Status: approved
Repo: `hail/` (core/api/voicebot) + `hail-website/` (new internal endpoint)

## Goal

Two independent disclosure lines exist today — the email footer/AI-disclosure
pair (`core/hailhq/core/email_footer.py`) and the voice AI-disclosure line
(`voicebot/hailhq/voicebot/agent.py`) — both read as generic, legalistic
disclaimers rather than natural copy, and the voice line never names the
business/person who requested the call.

Researching the voice gap surfaced a real regulatory requirement (not just a
tone question): 47 CFR § 64.1200(b)(1) requires artificial/prerecorded voice
calls to state the identity of the initiating business at the start of the
call, and the FCC's Feb 2024 declaratory ruling (FCC-24-17) confirmed
AI-generated voices — including real-time interactive ones, not just static
prerecorded clips — are "artificial voice" under the TCPA. This applies to
all message types (informational and marketing alike), not just telemarketing.
Email has no equivalent identity-naming mandate (CAN-SPAM's requirements are
different in kind — header accuracy, opt-out, physical address for commercial
mail), so the email change below is a pure copy/tone improvement, not a
compliance fix.

This spec covers three pieces: (1) a smaller/warmer email footer, (2) a
tightened generic voice fallback line, (3) speaking the actual org name on
voice calls when it can be resolved, closing the TCPA gap for the common
case.

## Decisions (locked)

1. **Email footer becomes one line, no lookup involved:** "Sent via Hail.so,
   an AI communication platform." — collapses `append_footer` +
   `append_disclosure`'s two separate paragraphs (see the screenshot this
   spec originated from) into one. `hail.so` stays a clickable link in HTML,
   plain text in the text-only body. Deliberately drops "on behalf of the
   sender" — no email-specific mandate requires that phrasing, and the
   remaining "AI communication platform" wording still discloses AI
   involvement.
2. **Voice generic fallback becomes:** "Hi, this is an AI assistant calling
   on behalf of whoever requested this call." (13 words, down from 17) —
   used whenever the org-name lookup (below) is unavailable, unresolvable,
   or the org has no name on file.
3. **Org name is looked up live, at call-creation time, not passed as a new
   API parameter and not injected via the LLM prompt.** Rejected
   alternatives: (a) a new `requester_name` field on `POST /calls` — pushes
   a compliance burden onto every API caller; (b) relaying the name via
   `system_prompt`/`VOICE_PREAMBLE` — the existing disclosure mechanism is
   deliberately a literal `session.say()`, not an LLM instruction, precisely
   because "the model could ignore" a prompt-relayed fact — a TCPA
   disclosure with a private right of action shouldn't depend on LLM
   compliance; (c) syncing/caching the name into a local table in `hail`'s
   own DB via a new website→hail webhook — more resilient (no live
   cross-service call on the critical path) but more moving parts (new
   table, new sync webhook, staleness handling) for a lookup that only
   needs to happen once per call, well before dial-out. Rejected in favor of
   a live call specifically because it's simpler to ship and the fail-safe
   design below removes the main risk (a stuck/down website blocking or
   degrading call placement).
4. **Lookup happens in the API (`POST /calls` route), before the call is
   placed — not in the voicebot after the recipient picks up.** A live-call
   delay between pickup and the greeting would be dead air; a slower
   `POST /calls` HTTP response is unnoticeable by comparison. The API
   already threads a metadata dict (containing `first_message`) down to the
   voicebot's room — the resolved name (or its absence) rides the same
   channel.
5. **Reuses the existing signed hail→hail-website internal-call pattern**
   (`core/hailhq/core/internal_webhook.py` — `X-Hail-Signature` HMAC over
   the POST body, `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET`, the same
   mechanism already used for the usage-events rater) instead of a new
   security scheme. That module is fire-and-forget (doesn't wait for/use a
   response); this needs a new function that does wait, with its own tight
   timeout.
6. **1000ms timeout, fail-safe to "no mention" on any failure** — timeout,
   non-2xx, connection error, malformed response body, or an org with no
   `name` on file — all collapse to the same outcome: `None`, meaning the
   voicebot uses decision 2's generic fallback line verbatim. No retries (a
   retry only delays call placement further with no guaranteed benefit).
   This is a strict floor, never a regression: worst case matches today's
   behavior exactly.

## Components

- **`hail-website`: new internal endpoint** — `POST /api/internal/organizations/lookup`,
  body `{organization_id}`, verified via the existing `X-Hail-Signature`
  scheme (same verification already used by `POST /internal/org-closures` /
  `POST /internal/dsar/*`). Reads `organizations.name` from Better Auth's own
  table. Returns `{name}` on success, 404 if the org doesn't exist.
- **`hail` core: new function** — `fetch_organization_name(organization_id: str) -> str | None`,
  living alongside (or as a sibling to) `internal_webhook.py`, reusing its
  `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET`/`sign()` plumbing. Signed POST,
  1000ms timeout, returns `None` on any failure — never raises. **Self-host
  parity with `internal_webhook.py`:** if `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET`
  is unset (self-hosted, no website counterpart configured), returns `None`
  immediately with no network attempt — same posture as the existing
  fire-and-forget notifier, and it means self-hosted voice calls always use
  decision 2's generic line, never attempt a lookup that has nowhere to go.
- **`hail` api: `POST /calls` route** — calls `fetch_organization_name`
  before creating the Call row / triggering the voicebot. Threads the result
  into the existing room-metadata dict alongside `first_message`.
- **`hail` voicebot: `speak_greeting()`** — reads the name from metadata. If
  present, speaks "Hi, this is an AI assistant calling on behalf of
  `{name}`." via the existing literal `session.say()` mechanism (still
  unconditional, still not reachable via `system_prompt`/`first_message`).
  If absent, speaks decision 2's generic line.
- **`core/hailhq/core/email_footer.py`** — `append_footer` and
  `append_disclosure` collapse into the single line from decision 1. Call
  sites in `api/hailhq/api/routes/emails.py` (lines ~387-390) are unaffected
  in shape — still two sequential calls, or collapse to one, at
  implementation-plan discretion — as long as the wire output is decision 1's
  single line.

## Data flow (voice)

1. `POST /calls` received, consent/validation passes as today.
2. API calls `fetch_organization_name(organization_id)` — signed POST to
   `hail-website`, 1000ms budget.
3. Success → org name string. Failure (any kind) → `None`.
4. Call row created, voicebot triggered, room metadata includes
   `{"org_name": <name or None>, "first_message": ...}` (key name at
   implementation-plan discretion).
5. Voicebot's `speak_greeting()` reads `org_name` from metadata at
   `session.start()` time (call already connected, recipient on the line) —
   this step does **no** network I/O, so it never adds latency here. It just
   picks which of the two pre-computed strings to speak.

## Error handling

Every failure mode in step 2 above is folded into one outcome (`None`) by
`fetch_organization_name` itself — the API route and voicebot never need to
distinguish _why_ a name is missing, only whether it's present. This keeps
the call site simple: one `if org_name:` branch, no error-type handling
leaking upward. `fetch_organization_name` logs failures (level: warning,
matching `internal_webhook.py`'s existing convention) for observability, but
never raises into the call-creation path.

## Testing

- **`hail-website`**: new endpoint returns the correct name for a valid org
  id; rejects requests with an invalid/missing signature; 404s for an
  unknown org id.
- **`hail` core**: `fetch_organization_name` — returns the name on a
  healthy 200; returns `None` on timeout, on 404, on 5xx, on a connection
  error, on a malformed (non-JSON or missing-`name`) response body, and when
  `HAIL_BASE_URL`/`HAIL_INTERNAL_SECRET` is unset (self-host, no network
  attempt made — assert via a mock that the HTTP client was never called).
  Six distinct test cases, one behavior.
- **`hail` api**: `POST /calls` — room metadata carries the resolved name
  when the lookup succeeds, and carries `None`/omits the key when it
  doesn't; existing call-creation tests unaffected (this is additive to the
  metadata dict, not a shape change to the response).
- **`hail` voicebot**: `speak_greeting()` — speaks the name-interpolated line
  when metadata has a name; speaks the generic fallback line when it
  doesn't (missing key, `None`, or empty string all treated the same).
- **`hail` core email**: existing footer/disclosure tests updated to assert
  the new single-line output; no behavior to test beyond string content
  (both branches — text-only and HTML body present — already have coverage
  to extend).

## Out of scope

- No change to _when_ the voice disclosure is spoken (still first, still
  unconditional, still before `first_message`).
- No change to consent enforcement, compliance-gate logic, or any other
  send-time check — this only affects the wording/content of an
  already-mandatory disclosure.
- No retry/backoff on a failed org-name lookup — a single attempt, fail-safe
  to the generic line, is the whole design.
- No caching/sync mechanism for the org name (decision 3's rejected
  alternative (c)) — out of scope for this pass; worth reconsidering later
  if live-lookup latency or `hail-website` availability becomes a practical
  problem, per the trade-off already discussed and rejected above.
