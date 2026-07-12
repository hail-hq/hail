# Voicebot agent tools — in-call send_sms / send_email / end_call / list_contacts

Status: approved
Owners: r13i
Repo: `hail/` (core/voicebot/api/openapi/cli/mcp)

## Goal

Give the voice agent tools drawn from Hail's own channels so it can act
mid-call: text the person it is talking to, email an org member, look up who
it may contact, and hang up gracefully. The mechanism is a channel-agnostic
registry — when a new modality ships (SMS today, others later), its tool
becomes available to the agent automatically, with no voicebot changes.

## Decisions (locked)

1. **v1 toolset: `send_sms`, `send_email`, `end_call`, `list_contacts`.**
   Curated for usefulness, each with guards matched to its risk (see catalog).
2. **`place_call` is rejected for v1.** An agent that can spawn calls is a
   recursion, cost-amplification, and harassment vector; it would need
   chain-depth tracking that does not exist. Revisit only with hard guards
   (no chaining, directory-only targets, per-call spawn cap).
3. **Tools are on by default, opt-out per call.** Every call gets every tool
   whose channel the org has configured (verified email domain → `send_email`;
   SMS live for the org → `send_sms`). `POST /calls` gains an optional
   `tools` field: a subset of tool names to allow, `[]` for none, omitted for
   all available. This is what makes a new modality automatically available.
4. **The agent never handles raw addresses.** Tool schemas accept only a
   directory reference (name) or, for SMS, the person on this call. The API
   resolves references to real addresses server-side. A jailbroken LLM has no
   parameter through which to target an arbitrary third party.
5. **Recipient directory = contacts table + org members.** The contacts table
   is being built in a separate workstream and is **out of scope here**; the
   directory treats it as one pluggable source and picks it up when it lands.
   v1 ships with the members source: website-mirrored `users` joined through
   `members`, always filtered by the call's `organization_id`.
6. **The call counterpart is an implicit SMS recipient.** "Text me the link"
   works without a directory entry — the recipient asked, on the record.
   Email has no counterpart equivalent: dictated addresses are unverifiable,
   so email recipients must exist in the directory.
7. **Architecture: core registry + execution through the API service** over
   an HMAC-signed internal route (existing `routes/internal/` +
   `hmac_signing.py` pattern). The full existing send stack — consent,
   suppression/velocity gate, funds, audit, AI-disclosure footer,
   unsubscribe header, billing — runs unchanged. Nothing is duplicated.
   Rejected: direct core calls from the voicebot (large refactor of a
   working route, voicebot would need provider creds, drift risk between two
   send paths); public API with a call-scoped token (token would ride LiveKit
   dispatch metadata, which is visible in the LiveKit Cloud dashboard).

## Architecture

### Tool registry — `core/hailhq/core/agent_tools/`

One module per tool exporting a `ToolSpec`:

- `name`, LLM-facing `description`, JSON parameter `schema`
- `risk_tier` — `read_only` | `session_control` | `outbound_send`
- `is_available(org_id, db)` — e.g. `send_email` checks for a verified email
  domain; `send_sms` returns False until the SMS channel is live for the org,
  then flips on with zero voicebot changes
- `execute(ctx, args)` — async; receives a `ToolContext` the voicebot
  supplies: internal-API client, DB session factory, `hangup()` callback,
  call info (`call_id`, `organization_id`, counterpart number)

`registry.all_tools()` collects the specs. Core stays livekit-free — specs
know nothing about LiveKit; the voicebot adapts them.

### Voicebot wiring — `voicebot/hailhq/voicebot/tools.py`

At call start: iterate the registry → filter by `is_available` (one DB query
per channel, voicebot's existing DB access) and the per-call `tools` opt-out
from dispatch metadata → wrap each spec as a LiveKit `function_tool` (raw
schema) → pass to `Agent(tools=...)`. Existing `function_tools_executed` →
`call_events` telemetry already covers tool-call logging.

### Internal API route — `POST /internal/agent/send-sms|send-email`

HMAC-signed request from the voicebot carrying `call_id`, tool args, and a
per-invocation idempotency key. The API:

1. verifies the signature,
2. loads the Call — must be `in_progress`; a tool call for an ended call is
   rejected,
3. resolves the recipient reference through the directory, scoped to the
   call's org (or the call's `to_number` for the SMS counterpart),
4. enforces the per-call send cap,
5. runs the existing consent/gate/funds/audit/billing send stack,
6. returns `{ok, spoken_summary}` or `{ok: false, spoken_error}` — a short
   plain sentence the agent can speak.

Consent posture for agent sends: the request happened on a recorded call
(`consent_source="voice_call"`, transactional `message_type`), set by the
route, never by the LLM. Suppression and velocity gates still run — a
suppressed recipient stays blocked even if they ask on-call.

### Recipient directory

One core lookup helper, used by both the voicebot (`list_contacts`) and the
internal route (recipient resolution), so scoping rules live in exactly one
place. Two sources behind it:

- **Contacts** (external workstream): rows scoped by their `organization_id`.
- **Org members**: `users` (website-mirrored) joined through `members`
  (`core/hailhq/core/models.py` — `OrganizationMember`), filtered by
  `members.organization_id = call org`. Core gains a read-only mapped `User`
  mirror, same posture as `OrganizationMember`. The implementation plan must
  verify the mirrored table's exact name and columns against the website
  schema before coding.

**Cross-org isolation rule (load-bearing):** every directory query starts
from the call's `organization_id` — contacts by their org FK, users only via
membership in that org. A user in two orgs is only findable through the org
that placed the call. Name collisions across sources return all matches
(name + source + channel presence); the agent disambiguates verbally.

## Tool catalog

| Tool            | Tier            | Guards                                                                                                                                                                                                                                                                                  |
| --------------- | --------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `end_call`      | session_control | Always available. Local only: waits for playout, then `ctx.shutdown()`. No API call; affects only its own call.                                                                                                                                                                         |
| `list_contacts` | read_only       | Always available. Runs in the voicebot via the shared core directory helper (direct DB read — no side effects, so no internal route). Returns names + channel presence (has email / has phone) — never raw addresses or numbers. Nothing leakable enters the transcript or LLM context. |
| `send_sms`      | outbound_send   | Recipient = directory ref or call counterpart only. Per-call send cap (default 5, counted server-side across both send tools by `metadata.call_id`). Full compliance gate. Body length-capped.                                                                                          |
| `send_email`    | outbound_send   | Recipient = directory ref only. Same cap, gate, audit; existing AI-disclosure footer + unsubscribe header apply unchanged.                                                                                                                                                              |

Also rejected for v1 (beyond `place_call`): any tool reading prior
calls/emails — cross-conversation exfiltration via a persuasive callee.

Attribution: sent rows carry `metadata.call_id`; audit actions
`agent.sms.send` / `agent.email.send` distinguish agent-initiated sends from
API-key-initiated ones.

## Prompt change

`VOICE_PREAMBLE` (`voicebot/hailhq/voicebot/agent.py`) gains one guardrail:
confirm recipient and content verbally before any send tool. Described
semantically on purpose — the preamble text may change in a parallel
workstream; the implementation adds the rule to whatever the guardrails
section then contains, rather than diffing today's text.

## API contract changes

- `POST /calls`: optional `tools: string[]` — validated against the registry,
  forwarded in dispatch metadata (names only, no secrets).
- New internal routes under `/internal/agent/` (not in the public OpenAPI
  surface if internal routes are excluded today; follow the existing
  internal-route posture).
- OpenAPI regen → prettier → CLI `make codegen`; MCP `place_call` gains the
  `tools` passthrough.

## Error handling

- A failed tool never drops the call; the agent receives a speakable error.
- Safety-check denials tell the agent "not allowed", without detail — no
  leaking suppression-list membership to the callee.
- One retry on timeout with the same idempotency key; no double sends.
- Tool call after call end → rejected (call not `in_progress`).

## Testing

- **Core**: registry unit tests — availability flips with org config, schemas
  valid, specs import no livekit.
- **API**: internal route — HMAC required; org scoping (no cross-org
  directory hits); cap enforced; gates still block; idempotent replay.
- **Voicebot**: extend existing fakes — tools appear/disappear per
  availability and opt-out; a tool error doesn't kill the session; `end_call`
  hangs up cleanly.
- **End-to-end**: fake call → `send_email` → Email row with
  `metadata.call_id`.

## Dependencies & risks

- **Contacts table** (separate workstream): not required for v1 — members
  source ships first; the contacts source activates when the table lands.
- **SMS channel** (separate spec, approved, unimplemented): `send_sms` is
  defined now but `is_available` stays False until SMS ships.
- **`users` mirror schema**: assumed to exist in the shared database (it is
  the website's table); name/columns must be verified during planning.
- **Per-call cap default (5)** is a code constant in v1; make it a setting
  only if someone asks.
