# BYO LLM endpoint — docs, ergonomics, guardrails, and a public provider API

Date: 2026-08-06
Status: implemented

## Problem

Hail already routes voice calls through a caller-supplied OpenAI-compatible
endpoint. Three brain modes exist and work
(`voicebot/hailhq/voicebot/pipeline.py`):

- **Mode A — house chain.** Caller sends `system_prompt`. Hail runs a
  `FallbackAdapter` over OpenAI → Google → Anthropic.
- **Mode B — per-call BYO.** Caller sends `llm: {base_url, api_key, model}`
  on `POST /calls`. Hail points `openai.LLM` at that endpoint for that call.
  No failover.
- **Mode C — standing org BYO.** Per-org rows (`llm`/`stt`/`tts`) written by
  the cloud console, read by the voicebot. Opt-in fallback to Hail's keys.

Precedence is B > C > A.

Nothing about the mechanism is missing. Four things around it are:

1. **It is undocumented.** Two sentences exist across the whole doc set —
   `docs/public/architecture.md:51` and `docs/public/cli.md:20`. Nothing
   states what Hail sends to the endpoint, what the endpoint must return,
   what URLs are accepted, or what happens when the endpoint fails. A user
   cannot adopt mode B from Hail's documentation alone.
2. **`system_prompt` and `llm` are mutually exclusive**
   (`core/hailhq/core/schemas.py`, `CallCreate._prompt_or_llm`). A mode B
   caller therefore cannot pass a prompt through Hail, and their endpoint
   receives only `VOICE_PREAMBLE` as the system message. The restriction is
   arbitrary: `build_instructions()`
   (`voicebot/hailhq/voicebot/agent.py`) is explicitly mode-agnostic and
   already composes preamble + caller prompt for either mode.
3. **A failing BYO endpoint runs to the wall clock.** Mode B has no
   failover by design, but also no give-up. An endpoint returning 500 on
   every turn produces a call where the agent repeats an error line until
   `HAIL_VOICE_MAX_DURATION_SECONDS` fires. The caller pays for the whole
   call.
4. **Standing org BYO config is console-only.** The routes live at
   `/internal/orgs/{organization_id}/providers`
   (`api/hailhq/api/routes/internal/provider_config.py`) behind
   shared-secret HMAC with `include_in_schema=False`. There is no public
   route, no CLI command, no SDK method, and no OpenAPI entry. A hosted
   customer managing configuration as code has to click through the
   console.

Prior art for 2 and 3: the opero-app branch
`feat/ra/eng-521-dispatch-voice-calls-from-chat-agent` injects a
`voice_prompt` into every proxied turn and aborts the call after
`MAX_CONSECUTIVE_ERRORS = 3` back-to-back failures. Its 200-turn cap is not
ported — Hail's wall-clock soft cap already bounds call length.

## Scope

Four deliverables, four PRs, in this order. Deliverable 1 ships alone and
unblocks users without touching runtime code.

Out of scope: changing mode B's no-failover semantics; changing mode C's
opt-in fallback; adding new BYO layers; any self-host-specific path
(self-hosters configure providers by env var and are unaffected by
deliverable 4).

---

## Deliverable 1 — `docs/public/byo-llm.md`

A new page, registered in `docs/public/meta.json` immediately after
`architecture`. Agent-first per the repo tenet: it opens with something
runnable and links canonical sources rather than paraphrasing them.

### Structure

1. **Run it in five minutes.** A complete FastAPI application (~40 lines)
   that implements the endpoint contract, followed by the exact
   `hail call` invocation that drives a real call through it. The example
   returns a fixed reply so the reader confirms the wiring before writing
   any logic. Ported from opero's `_emit_openai_stream`
   (`apps/api/src/api/v1/dispatch.py`), adapted to stand alone.

2. **The wire contract.** Hail's `openai.LLM` wraps the official `openai`
   Python client, so per voice turn Hail issues:
   - `POST {base_url}/chat/completions`
   - `Authorization: Bearer {api_key}`
   - `stream: true`
   - `messages`: the running conversation, with Hail's composed
     instructions as the leading `system` message
   - `tools`: JSON Schema for the call's enabled agent tools, when the call
     has any

   The endpoint must respond `text/event-stream` with OpenAI
   `chat.completion.chunk` frames terminated by `data: [DONE]`. Link to
   `voicebot/hailhq/voicebot/pipeline.py` (`build_llm`) as the canonical
   source rather than restating it.

   **Verification requirement:** before this section is written, run the
   example endpoint against a real Hail call and log the received request
   body and headers. The documented contract must be transcribed from that
   capture, not from the plugin source or from memory. The captured payload
   is committed alongside the doc as a fenced block.

3. **URL requirements.** `https` only; publicly resolvable host. The
   syntactic check runs in `LLMConfig` at request validation; the resolving
   SSRF check (`assert_public_https_url`) runs off the event loop in the
   API route and again in the voicebot at call time. A rejected URL fails
   the request at `POST /calls`; a URL that resolves privately by call time
   ends the call with `end_reason=provider_key_error`.

4. **Failure semantics.** Mode B has no failover: if the endpoint is down,
   the call ends. Mode C fails over to Hail's keys only when
   `fallback_enabled` is set. Cross-reference deliverable 3's new end
   reason once that lands.

5. **Per-call vs standing config.** Per-call (`curl`, `hail call`, Python
   SDK, MCP `place_call`) for one-off or per-tenant brains; console
   Providers page for a standing default. Table of precedence B > C > A.

### Acceptance

- The FastAPI example runs as written, against a real call, before commit.
- Every code block is copy-pasteable with no elision.
- The page is reachable from `docs/public/meta.json` and linked from
  `architecture.md:51` and `cli.md:20`.

---

## Deliverable 2 — allow `system_prompt` alongside `llm`

Remove the mutual exclusion; keep the requirement that at least one of the
two is present.

### Changes

- `core/hailhq/core/schemas.py` — `CallCreate._prompt_or_llm`: drop the
  both-present error, keep the neither-present error.
- `sdk/hail/models.py` — `CallCreate` validator: same change; update the
  class docstring, which currently describes the two as exclusive modes.
- `sdk/hail/client.py` — `calls.create` docstring.
- `cli/internal/cmd/call.go` — drop the
  `--prompt and --llm-* are mutually exclusive` guard; keep the
  "one of the two is required" guard and keep the all-three-or-none rule on
  `--llm-url`/`--llm-key`/`--llm-model`.
- `mcp/hailhq/mcp/tools.py` — `place_call` docstring.
- `openapi/openapi.yaml` — regenerate, then
  `pnpm exec prettier --write openapi/openapi.yaml`.
- `cli/` — `make codegen` after the spec is regenerated.

No voicebot change: `build_instructions()` already handles the combination.

### Tests

- `core/tests/test_schemas.py` — both fields present validates; neither
  still raises.
- `sdk/tests/test_models.py` — mirror.
- `cli/internal/cmd/call_test.go` — replace the mutual-exclusion assertion
  with one asserting the combined request body carries both
  `system_prompt` and `llm`.
- `api/tests/test_calls_api.py` — `POST /calls` with both fields returns
  201 and both land in dispatch metadata.
- `voicebot/tests/test_agent.py` — a mode B call with a `system_prompt` in
  metadata produces instructions containing both the preamble and the
  caller text.

---

## Deliverable 3 — give up on a persistently failing BYO endpoint

### Mechanism

Count on the existing `session.on("error")` event in `agent.py`, beside the
handlers already registered there. Do **not** wrap or subclass the plugin's
`LLM`.

The event carries a `livekit.agents.llm.LLMError`, which has a
`recoverable: bool` field. Count only `recoverable=False` events. Reset the
counter on `conversation_item_added` for an assistant message — proof that a
turn completed.

This choice is the safety property of the deliverable. A caller interrupting
the agent (barge-in) cancels the in-flight LLM stream with
`asyncio.CancelledError`, which never surfaces as an `LLMError`. A counting
wrapper around `chat()` would see that cancellation as a failure and could
hang up a healthy, talkative call. The event path cannot: LiveKit has
already classified the failure before Hail sees it.

On the third consecutive non-recoverable error, speak a fixed goodbye line
via `session.say()`, stamp
`end_reason=CallEndReason.LLM_ENDPOINT_FAILED`, and call `ctx.shutdown()` —
the same sequence `soft_cap_announce_and_hangup` already uses.

Threshold: 3, matching opero's `MAX_CONSECUTIVE_ERRORS`. Hardcoded, not an
env var — one transient failure must be tolerated, and persistent failure
must end promptly; there is no operator decision between those.

Applies to mode B only. Mode C with `fallback_enabled` already fails over;
mode C without it is a deliberate fail-fast on a config the org owns and
already surfaces as `provider_key_error` at build time.

### Changes

- `core/hailhq/core/call_end_reasons.py` — add
  `LLM_ENDPOINT_FAILED = "llm_endpoint_failed"` under the agent-failure
  group.
- `api/migrations/versions/` — new migration adding the enum value to the
  `call_end_reason` Postgres type. Follow the pattern in
  `0024_provider_key_error_reason.py`.
- `voicebot/hailhq/voicebot/pipeline.py` — the adapter and its wiring in
  `build_llm`.
- `voicebot/hailhq/voicebot/agent.py` — the on-exhausted callback, next to
  the soft-cap wiring.
- `openapi/openapi.yaml` — regenerate if the end reason is enumerated in
  the call schema; prettier; `make codegen`.

### Tests

`voicebot/tests/test_pipeline_byo.py`:

- Two failures then a success resets the counter; no callback fires.
- Three consecutive failures fire the callback exactly once.
- A fourth failure after exhaustion does not fire it again.
- Mode A and mode C construction paths are unwrapped.

---

## Deliverable 4 — public org provider config API, CLI, and SDK

Bring standing org BYO config to parity with the console for hosted
customers who manage configuration as code.

### Security position

Reads never return a key — only `key_last4` and `key_set_at`, as the
internal route already does. Writes under an API key let a leaked key
repoint an org's LLM; that same leaked key can already do so per-call via
`llm.base_url`, so the marginal exposure is nil. Full CRUD is public. No
feature flag, no self-host special case.

### API

New public router at `/providers`, API-key authenticated, organization
resolved from the key — never from the path. The organization ID does not
appear in the public route, so one org's key cannot address another's
config.

- `GET /providers` — list all layers.
- `PUT /providers/{layer}` — upsert and activate.
- `DELETE /providers/{layer}/{provider}` — delete, promoting a sibling.
- `POST /providers/{layer}/activate` — switch active provider.
- `POST /providers/{layer}/validate` — live key test.

`api/hailhq/api/routes/internal/provider_config.py` is **not refactored**.
The console runs on that file in production; extracting shared handlers out
of it to serve a new route puts a working page at risk for no user-visible
gain. The public router is a new file that imports and calls the existing
functions, passing the organization resolved from the API key. The internal
HMAC router keeps its org-in-path form, which is what makes it internal.

If duplication between the two routers becomes real (not anticipated —
the public router is a thin auth-and-scope layer), consolidate in a later
PR with the console's tests as the gate.

Request and response models move into `core/hailhq/core/schemas.py` so the
SDK and the OpenAPI spec share them.

### CLI

New `hail providers` command group in `cli/internal/cmd/`:

- `hail providers list`
- `hail providers set <layer> --provider <p> --model <m> [--base-url <u>] [--key <k>] [--fallback]`
- `hail providers delete <layer> <provider>`
- `hail providers activate <layer> --provider <p>`
- `hail providers test <layer> [--provider <p>]`

`--key` reads from stdin when passed as `-`, so keys stay out of shell
history.

### SDK

`client.providers.list()`, `.set()`, `.delete()`, `.activate()`,
`.test()` in `sdk/hail/client.py`, with models in `sdk/hail/models.py`.

### Docs

Extend `docs/public/byo-llm.md`'s standing-config section with the CLI and
SDK paths beside the console click path. Add the command group to
`docs/public/cli.md`.

### Tests

- `api/tests/test_provider_config_public.py` — auth required; org scoping
  proven by asserting a key for org A cannot read or write org B's rows;
  each verb's happy path and its 404/409/422 cases.
- `api/tests/test_internal_provider_config.py` — unchanged and still
  passing, proving the extraction preserved behavior.
- `cli/internal/cmd/providers_test.go` — flag parsing, request body shape,
  stdin key reading.
- `sdk/tests/test_client.py` — request shapes for each method.

---

## Sequencing

1. Deliverable 1 — docs. No runtime risk; merges independently.
2. Deliverable 2 — schema relaxation. Small, touches five surfaces plus
   codegen.
3. Deliverable 3 — guardrail. Needs an enum migration; deploy the migration
   before the voicebot image that emits the new reason.
4. Deliverable 4 — public provider API. Largest; strictly additive.

Each is a separate PR against `main`.
