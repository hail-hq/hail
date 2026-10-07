# Agents and inbound calls and texts: implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Numbers answer calls and texts through a saved agent, on every carrier, with webhooks, billing and the AI line, plus a console rebuilt around agents.

**Architecture:** One `agents` table owns the brain settings. `phone_numbers` points at a voice agent and a text agent. LiveKit dispatches the existing `hail-voicebot` worker into inbound rooms with static metadata `{"direction":"inbound"}`; the worker reads the SIP attributes, opens the `Call` row through core, and runs the shared session code. Texts are answered by a text worker in the voicebot process that reuses `build_llm`. Carrier work goes through `carrier_routing.Carrier`.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, livekit-api 1.2.0 (`update_inbound_trunk_fields`), livekit-agents 1.6.6, Twilio SDK (`trunking.v1`), httpx (Telnyx, DIDWW), Next.js console (hail-website), vitest, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-agents-inbound-design.md`

## Global Constraints

- New env vars land in `.env.example` in the same commit as `config.py`.
- Provider SDK calls live in `core/hailhq/core/providers/<channel>/<name>.py`; `api/` and `voicebot/` import core only.
- After any route change: regenerate `openapi/openapi.yaml` and `cli` codegen in the same PR (codegen in its own commit).
- Migrations continue from `0049_audit_actor.py` → `0050_agents_inbound.py`.
- Conventional Commits. No AI attribution trailers.
- Python: ruff + black; `uv run pytest` per package. Never `ruff format` whole trees.
- Console copy: ASD-STE100, no em dash. `pnpm run build` is the last check for `app/` changes; screenshot UI changes.

## Review Focus

1. An INVITE for a number that belongs to another org or is a pool number must be dropped with no `Call` row (test in Task 6).
2. A duplicate `participant_attributes_changed` on inbound must not write two `call.answered` events (`mark_call_answered` guard; test in Task 7).
3. `PATCH /numbers/{id}` with the same agent twice must not re-add the number to the LiveKit trunk or re-attach at the carrier (test in Task 5).
4. A STOP text to a number with a text agent must get the compliance reply and no agent reply (test in Task 9).
5. Deleting an agent that numbers point at must unregister those numbers for inbound and null the FKs (test in Task 4).

---

### Task 1: Schema and migration 0050

**Files:**

- Modify: `core/hailhq/core/models.py` (Agent, PhoneNumber, Call, Sms, OrganizationCallSettings)
- Modify: `core/hailhq/core/call_end_reasons.py` (`INSUFFICIENT_FUNDS`, `NO_AGENT`)
- Create: `api/migrations/versions/0050_agents_inbound.py`
- Test: `core/tests/models/test_agent_shape.py`, `core/tests/test_call_end_reasons.py` (extend)

**Produces:** `Agent` model with columns from the spec; `PhoneNumber.voice_agent_id`, `sms_agent_id`, `inbound_registered_at`; `Call.to_number_id`, `Call.agent_id`, nullable `from_number_id`; `Sms.agent_id`, `agent_reply_state`; `OrganizationCallSettings.ai_disclosure_line`, nullable `max_duration_seconds`.

- [ ] Write `test_agent_shape.py`: create an org, an agent, a number pointing at it; delete the agent; the number's `voice_agent_id` is null (ON DELETE SET NULL).
- [ ] Write the models and the migration (alter enum with `ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS`, outside a transaction block where needed, as 0003/0046 did).
- [ ] `cd core && uv run pytest tests/models -q` → pass. `cd api && uv run alembic upgrade head` against local Postgres → pass.
- [ ] Commit: `feat(db): agents, number routing, inbound call and text columns (0050)`

### Task 2: Schemas and settings

**Files:**

- Modify: `core/hailhq/core/schemas.py` (AgentCreate, AgentUpdate, AgentResponse, AgentListResponse, PhoneNumberRoutingUpdate, PhoneNumberResponse fields, CallCreate.agent_id, CallResponse.agent_id, SmsResponse.agent_id, `call.received` in `WEBHOOK_EVENT_TYPES`)
- Modify: `core/hailhq/core/config.py` + `.env.example` (`livekit_telnyx_sip_inbound_trunk_id`, `livekit_didww_sip_inbound_trunk_id`, `twilio_sip_trunk_sid`, `didww_voice_in_trunk_id`)
- Test: `core/tests/schemas/test_agent_schemas.py`

**Produces:** `AgentCreate(name, system_prompt, first_message=None, ai_disclosure=True, ai_disclosure_line=None, voice_config=VoiceConfig(), tools=None, max_duration_seconds=None, sms_enabled=True, status="live")`; `AgentUpdate` all optional; `CallCreate._prompt_or_llm` accepts `agent_id` alone.

- [ ] Tests: `AgentCreate` rejects an empty name and a 2000-char `ai_disclosure_line`; `CallCreate(to=..., agent_id=...)` validates without `system_prompt`.
- [ ] Implement; run `cd core && uv run pytest tests/schemas -q`.
- [ ] Commit: `feat(core): agent schemas, agent_id on calls, inbound settings`

### Task 3: Disclosure line helpers

**Files:**

- Create: `core/hailhq/core/disclosure.py`
- Modify: `voicebot/hailhq/voicebot/agent.py` (`disclosure_line` delegates; `speak_greeting` reads `ai_disclosure_line` and `direction` from metadata)
- Test: `core/tests/test_disclosure.py`, `voicebot/tests/test_agent.py` (extend)

**Produces:** `disclosure_text(direction: Literal["outbound","inbound"], org_name: str | None, template: str | None) -> str`. Defaults: outbound `Hi, this is an AI assistant calling on behalf of {org}.`; inbound `Hi, this is an AI assistant answering on behalf of {org}.`; no org name → `{org}` becomes `whoever requested this call` (outbound) / `this number` (inbound). A template without `{org}` is spoken as is.

- [ ] Tests for the four combinations plus a custom template.
- [ ] Implement; keep `AI_DISCLOSURE_LINE` exported for existing tests.
- [ ] `cd voicebot && uv run pytest tests/test_agent.py -q -k disclos`.
- [ ] Commit: `feat(voicebot): direction-aware AI line with workspace and agent templates`

### Task 4: Agents API

**Files:**

- Create: `api/hailhq/api/routes/agents.py`
- Modify: `api/hailhq/api/main.py` (include router), `api/hailhq/api/route_prefixes.py` if it lists resources
- Test: `api/tests/test_agents_api.py`

**Produces:** `POST /agents` 201, `GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}`, `DELETE /agents/{id}` 204. Delete calls `inbound_routing.unregister` for each number pointing at the agent (Task 5) then deletes.

- [ ] Tests: create/list/get/patch/delete; 404 across orgs; duplicate name 409; delete detaches numbers (Review Focus 5).
- [ ] Implement with `get_current_principal`, `write_audit_log("agent.create" | "agent.update" | "agent.delete")`.
- [ ] `cd api && uv run pytest tests/test_agents_api.py -q`.
- [ ] Commit: `feat(api): agents CRUD`

### Task 5: Inbound registration and number routing

**Files:**

- Modify: `core/hailhq/core/carrier_routing.py` (`Carrier.inbound_trunk`, `attach_inbound`, `detach_inbound`; `inbound_trunk(provider)`, `carrier_for_inbound_trunk(trunk_id)`)
- Modify: `core/hailhq/core/providers/voice/twilio.py` (`attach_inbound_number(resource_id)`, `detach_inbound_number(resource_id)` via `client.trunking.v1.trunks(settings.twilio_sip_trunk_sid).phone_numbers`)
- Modify: `core/hailhq/core/providers/voice/telnyx.py` (`attach_inbound_number(resource_id)` → `PATCH /v2/phone_numbers/{id}/voice {"connection_id": settings.telnyx_connection_id}`; detach sends `""`)
- Modify: `core/hailhq/core/providers/voice/didww.py` (`attach_inbound_number(did_id)` → `PATCH /v3/dids/{id}` relationships `voice_in_trunk`; detach null) — after hail#132 lands; until then raise `CarrierNotConfigured`
- Modify: `core/hailhq/core/livekit.py` (`add_inbound_number(trunk_id, e164)`, `remove_inbound_number(trunk_id, e164)` via `self._lkapi.sip.update_inbound_trunk_fields(trunk_id, numbers=ListUpdate(add=[e164]))`)
- Create: `core/hailhq/core/inbound_routing.py` (`register(db, lk, number)`, `unregister(db, lk, number)`)
- Modify: `api/hailhq/api/routes/numbers.py` (`PATCH /numbers/{id}`; release path calls `unregister` first)
- Test: `core/tests/test_carrier_routing.py` (extend), `core/tests/test_inbound_routing.py`, `api/tests/test_numbers_api.py` (extend)

- [ ] Tests: `register` is idempotent (second call does nothing when `inbound_registered_at` is set; Review Focus 3); `unregister` clears the stamp; `carrier_for_inbound_trunk` maps all three and raises on unknown; `PATCH /numbers/{id}` with `voice_agent_id` registers, with `null` unregisters, with an agent from another org → 404; `voice_agent_id` on a number without `voice` → 422; `sms_agent_id` on a number without `sms` → 422.
- [ ] Implement.
- [ ] `cd core && uv run pytest -q tests/test_carrier_routing.py tests/test_inbound_routing.py`; `cd api && uv run pytest tests/test_numbers_api.py -q`.
- [ ] Commit: `feat(numbers): route calls and texts to an agent; register inbound at the carrier and LiveKit`

### Task 6: Open an inbound call (core)

**Files:**

- Create: `core/hailhq/core/inbound_calls.py`
- Modify: `core/hailhq/core/webhook_fanout.py` (`call_event_data(call)` helper: id, status, direction, from, to, agent_id, end_reason)
- Test: `core/tests/test_inbound_calls.py`

**Produces:** `open_inbound_call(db, *, dialed: str, caller: str, trunk_id: str, room_name: str, provider_call_sid: str | None) -> InboundOutcome` where `InboundOutcome = Accepted(metadata: dict) | Rejected(reason: str, call_id: UUID | None)`. Writes rows and webhooks as the spec says; fetches the org name through `fetch_organization_name` and the workspace AI line from `organization_call_settings`.

- [ ] Tests: unknown number → `Rejected("unknown_number")`, no row (Review Focus 1); pool number → same; wrong trunk for the carrier → same; no agent → `Call` failed `no_agent` + `call.failed`; paused agent → same; no funds → `insufficient_funds`; happy path → `Call` ringing, `call.received`, metadata carries agent fields and `direction="inbound"`.
- [ ] Implement.
- [ ] `cd core && uv run pytest tests/test_inbound_calls.py -q`.
- [ ] Commit: `feat(core): open inbound calls from SIP attributes`

### Task 7: Voicebot inbound branch

**Files:**

- Modify: `voicebot/hailhq/voicebot/agent.py` (`parse_metadata`, `entrypoint` inbound branch, skip AMD when `direction == "inbound"`)
- Test: `voicebot/tests/test_agent_inbound.py`

- [ ] Tests with `_fakes.py`: `parse_metadata('{"direction":"inbound"}')` returns no `call_id`; inbound entrypoint with a rejected outcome deletes the room and starts no session; accepted outcome starts the session, skips AMD, speaks the inbound line, and a duplicate attribute event marks answered once (Review Focus 2).
- [ ] Implement.
- [ ] `cd voicebot && uv run pytest -q`.
- [ ] Commit: `feat(voicebot): answer inbound calls`

### Task 8: Outbound calls through an agent

**Files:**

- Modify: `api/hailhq/api/routes/calls.py` (resolve `agent_id`, fill body fields, stamp `calls.agent_id`, pass `ai_disclosure_line` and `direction="outbound"` in dispatch metadata)
- Test: `api/tests/test_calls_api.py` (extend)

- [ ] Tests: `POST /calls` with `agent_id` and no prompt → 201 and dispatch metadata holds the agent's prompt; explicit `first_message` wins; agent from another org → 404.
- [ ] Implement; `cd api && uv run pytest tests/test_calls_api.py -q`.
- [ ] Commit: `feat(calls): place outbound calls with agent_id`

### Task 9: Text replies

**Files:**

- Modify: `core/hailhq/core/sms_ingest.py` (mark `agent_reply_state='pending'`)
- Create: `core/hailhq/core/text_agent.py` (`claim_pending_reply(db) -> Sms | None`, `thread_messages(db, sms, limit=20)`, `TEXT_PREAMBLE`, `build_chat_messages(agent, history)`, `finish_reply(db, sms, state)`)
- Create: `voicebot/hailhq/voicebot/textbot.py` (`run_forever()` loop; `reply_once(sms)` uses `resolve_org_configs` + `build_llm` + `AgentApiClient.post("/internal/agent/reply-sms", ...)`)
- Modify: `voicebot/hailhq/voicebot/main.py` (start the loop in a daemon thread when `HAIL_INTERNAL_SECRET` is set)
- Modify: `api/hailhq/api/routes/internal/agent.py` (`POST /internal/agent/reply-sms`)
- Test: `core/tests/test_sms_ingest.py` (extend), `core/tests/test_text_agent.py`, `voicebot/tests/test_textbot.py`, `api/tests/test_internal_agent.py` (extend)

- [ ] Tests: STOP on a routed number → compliance reply, `agent_reply_state` stays null (Review Focus 4); plain text on a routed number → `pending`; `build_chat_messages` orders history oldest first and caps at 20; 21st agent reply in 24 h → `skipped`; `reply-sms` route sends through the number's carrier and sets `sms.agent_id`.
- [ ] Implement; run the four test files.
- [ ] Commit: `feat(sms): agents answer inbound texts`

### Task 10: OpenAPI, CLI, MCP, SDK

**Files:**

- Regenerate `openapi/openapi.yaml` (`cd api && uv run python -m hailhq.api.openapi_dump` or the repo's documented command in `docs/public/contributing.md`)
- `cd cli && make codegen` (own commit)
- Modify: `mcp/hailhq/mcp/tools.py` (`list_agents`, `create_agent`, `route_number`), `sdk` client methods if generated by hand
- Test: `mcp/tests/test_tools.py` (extend)

- [ ] Commit 1: `chore(openapi): agents, number routing, agent_id on calls`
- [ ] Commit 2: `chore(cli): regenerate client`
- [ ] Commit 3: `feat(mcp): agent tools`

### Task 11: Docs and changelog

**Files:**

- Create: `docs/public/agents.md`
- Modify: `docs/public/self-host/twilio.md`, `telnyx.md`, `didww.md`, `livekit-cloud.md` (inbound trunk JSON, dispatch rule JSON, provider steps), `docs/public/architecture.md`, `docs/public/webhooks.md`, `README.md`, `CLAUDE.md`, `CHANGELOG.md`

- [ ] Write; `cd docs-site && pnpm run build` if the docs site builds locally.
- [ ] Commit: `docs: agents and inbound calls and texts`

### Task 12: Console: agents

**Files (hail-website):**

- Modify: `app/console/SideNav.tsx` (Agents group)
- Create: `app/console/agents/page.tsx`, `AgentsList.tsx`, `actions.ts`, `new/page.tsx`, `[id]/page.tsx`, `AgentSheet.tsx`, `agent-types.ts`, `QrCode.tsx` (uses the `qrcode` npm package; add it)
- Modify: `app/console/console.css` (sheet styles from the mockup: `.ag-step`, `.ag-say`, `.ag-rail`, `.ag-qr`)
- Test: `app/console/agents/__tests__/agent-form.test.ts` (form → API body mapping), `app/console/__tests__/server-action-exports.test.ts` (new actions are async)

- [ ] Build; screenshot desktop and 390px; commit: `feat(console): agents list and agent sheet`

### Task 13: Console: numbers routing, call settings AI line, activity

**Files (hail-website):**

- Modify: `app/console/numbers/NumbersPanel.tsx`, `actions.ts` (`routeNumberAction(numberId, {voice_agent_id?, sms_agent_id?})`)
- Modify: `app/console/calls/CallDurationPanel.tsx` → add `AiLinePanel.tsx`; `app/console/calls/actions.ts` (`updateAiLineAction`); `lib/hail-internal.ts` (`getEffectiveCallSettings` returns `ai_disclosure_line`)
- Modify: `app/console/activity/*` (agent column, inbound call trail rows)
- Test: `app/console/calls/__tests__/ai-line.test.ts`

- [ ] Build; screenshots; commit: `feat(console): route numbers to agents, AI line, inbound activity`

### Task 14: Landing copy

**Files (hail-website):**

- Modify: `app/(marketing)/voice/page.tsx`, `lib/home-products.ts`, `app/(marketing)/pricing/page.tsx`, `lib/about-copy.ts`, `app/(marketing)/compare/competitors.tsx`, `app/llms.txt/route.ts`, `app/skill.md`, `lib/agent-skill-doc.ts`, `content/legal/facts.md`
- Test: `app/__tests__/discoverability.test.ts` still passes

- [ ] Replace outbound-only claims; build; `curl` the inline-space check from AGENTS.md; commit: `feat(marketing): inbound calls and texts copy`

### Task 15: PRs

- [ ] hail: push `feat/inbound-calls`, open PR to `main` (title `feat: agents answer inbound calls and texts`), body lists env vars and LiveKit setup.
- [ ] hail-website: push `feat/inbound-calls`, open PR to `master`, note it merges after the hail PR deploys.
