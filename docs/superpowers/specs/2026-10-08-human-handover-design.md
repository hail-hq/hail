# Human handover: the voice agent hands a call to a person

## Goal

During a call, the agent can connect the caller to a person the admin chose.

- The admin picks, per agent, which contacts may receive a handover, and when to use each.
- Nobody outside that list can be dialed. The LLM never sees a phone number.
- The caller is never dropped: if the person does not answer, the agent comes back.
- Every minute of the call, before and after the handover, is billed as today.

## Decisions (user, 2026-10-07/08)

| Topic | Decision |
|---|---|
| Mechanism | Bridge: Hail dials the person into the same LiveKit room. No SIP REFER. |
| Targets | Existing `contacts`, picked per agent, each with a "when to use" note. |
| Scope | Saved agents only. `POST /calls` with `agent_id` inherits the list. No per-call override. |
| Caller ID shown to the person | The Hail number on the call. Never the caller's number. |
| No answer / busy | After 30 s of ringing, the agent comes back and offers to take a message. |
| Agent after connect | Says one line to both, then mutes its mic and stops listening. Stays in the room. |
| Time limit | `max_duration_seconds` applies until the contact answers (agent talking, contact ringing). Once the contact answers, it stops applying: the call runs until the caller or the contact hangs up. |
| Person hangs up first | The whole call ends. |
| Safety check | `check_call_allowed` (DNC, premium rate) when the admin saves the agent AND right before dialing. |
| Billing | Unchanged: one call, all minutes until the last hang-up, normal voice rate (10¢/min). After the handover the AI is muted, so its STT/LLM/TTS cost stops and covers the second phone leg. |
| Destination guard | The contact's country must be one Hail sells numbers in (`costs/<carrier>.json`). Blocks costly countries the flat rate does not cover. |
| Webhook | New `call.transferred`, sent when the person answers. |

## Today (verified in code)

- No transfer code exists. `docs/superpowers/specs/2026-10-02-agents-inbound-design.md` listed it as a non-goal.
- The voicebot reads call config only from dispatch metadata (`voicebot/hailhq/voicebot/agent.py`, `parse_metadata`). Metadata is built at `api/hailhq/api/routes/calls.py` (outbound) and `core/hailhq/core/inbound_calls.py` (`open_inbound_call`).
- Tools: `core/hailhq/core/agent_tools/` (`ToolSpec`, `ToolContext`, `registry.py`). Transport handles are callables injected from `agent.py` (`make_agent_hangup`, `make_agent_send_dtmf`), so `core` stays free of LiveKit.
- `list_contacts` already hides raw addresses from the LLM. This feature follows the same rule.
- Outbound caller leg identity is `caller-{call_id}` (AMD keys on it). Inbound identity is set by LiveKit.
- Any SIP participant leaving ends the session (`_on_participant_disconnected`).
- Carrier trunk per number: `carrier_routing.voice_route(provider)` returns `(trunk_id, headers)`. A number never leaves its carrier.
- Installed `livekit-api`: `CreateSIPParticipantRequest` has `wait_until_answered`, `ringing_timeout`, `sip_number`, `play_dialtone`. A failed dial raises `SipCallError` with the SIP status.
- Billing: one `usage_events` row per call, written in `on_call_end`, duration from `answered_at` to end.

## Design

### 1. Data

New table `agent_handover_contacts` (migration `0053`):

| Column | Type | Notes |
|---|---|---|
| `agent_id` | uuid FK `agents.id` ON DELETE CASCADE | |
| `contact_id` | uuid FK `contacts.id` ON DELETE CASCADE | |
| `note` | text NOT NULL | "When to use", 1–200 chars. Shown to the LLM. |
| `position` | smallint NOT NULL | Order in the list, from 0. |
| `created_at` | timestamptz | |

- Primary key `(agent_id, contact_id)`.
- Max 10 rows per agent.
- Contact name comes from `contacts.name` at call time, so a rename shows up on the next call.
- A contact whose phone is removed stays linked but is skipped at call time.

### 2. API

Agent schemas (`core/hailhq/core/schemas.py`):

- `AgentCreate`, `AgentUpdate`: `handover_contacts: list[{contact_id, note}] | None`. List order = `position`. On update, the list replaces the old one.
- `AgentResponse`: `handover_contacts: [{contact_id, name, phone_e164, note}]`.
- Validation on save (`api/hailhq/api/routes/agents.py`):
  - contact belongs to the org, has `phone_e164`, no duplicates, max 10;
  - `check_call_allowed(db, org, phone)` passes;
  - the phone's country (`phonenumbers`, already a core dependency) has a row in any carrier catalog (`costs/<carrier>.json`).
  - Else 422 naming the contact.

Dispatch metadata (outbound in `calls.py`, inbound in `inbound_calls.py`) gains:

- `handover_targets: [{contact_id, label, note}]` — no numbers. `label` is the contact name; a repeated name gets " (2)", " (3)".

New internal endpoint `POST /internal/agent/handover` (`api/hailhq/api/routes/internal/agent.py`, same auth as `send_sms`):

- Request: `{call_id, contact_id}`.
- Checks: the call is live; its agent still links this contact; contact has a phone; `check_call_allowed` passes; the phone's country has a row in the call's carrier catalog (`costs/<call.provider>.json`); no earlier connected handover on this call.
- Response: `{to_e164, from_e164, trunk_id, headers}` or a denial reason the agent can say. `from_e164` is the Hail number on the call; `trunk_id`/`headers` come from `voice_route(call.provider)`, so metadata needs no number or carrier.

New internal endpoint `POST /internal/agent/handover-result`: `{call_id, contact_id, outcome, sip_status, ring_ms}`. Writes the `handover` call event and, on `answered`, the webhook.

Webhook:

- Add `call.transferred` to the event type list (`schemas.py`).
- Payload: the call object plus `transfer: {contact_id, contact_name}`.
- Sent once, when the person answers.

OpenAPI regenerated; CLI (`make codegen`, `cli/internal/cmd/agents.go`) and MCP (`mcp/hailhq/mcp/tools.py`) gain the agent field.

### 3. Voicebot

New tool `transfer_call` (`core/hailhq/core/agent_tools/transfer_call.py`):

- Parameters:
  - `contact` — enum of the target names from metadata;
  - `reason` — one short sentence for the person ("an invoice question"). Capped at 200 chars.
- Description lists each name with its note: "Sam (billing questions)".
- `is_available`: false when `handover_targets` is empty.
- `risk_tier="session_control"`, so the agent finishes speaking first.
- Registered in `registry.py`; console list in `hail-website/lib/agent-tools.ts`.

New `ToolContext.bridge` handle, built in `agent.py` as `make_agent_bridge`:

1. Agent says "One moment, I'm connecting you." (the LLM's own words before calling the tool).
2. Call `/internal/agent/handover`. Denied → return the reason; the agent tells the caller.
3. `create_sip_participant` with `sip_number=from_e164`, trunk from the response, identity `human-{call_id}`, `wait_until_answered=True`, `ringing_timeout=30s`, `play_dialtone=True`.
4. Answered:
   - write `call_events` kind `handover`, payload `{contact_id, name, outcome: "answered", ring_ms}`;
   - fan out `call.transferred`;
   - agent says one line to both: "Hi {name}, I have a caller on the line. They say it is about {reason}. Connecting you now." (`{name}` without the " (n)" suffix; without a reason: "Hi {name}, I have a caller on the line. Connecting you now.");
   - disable the session's audio input and output; the agent stays in the room;
   - the tool returns "Connected. They are talking now. Say nothing and do not end the call." and `end_call` does nothing while connected.
5. `SipCallError` or timeout:
   - write `handover` event with `outcome: "no_answer" | "busy" | "failed"` and the SIP status;
   - return "They could not pick up." The agent offers to take a message.
6. Caller hangs up while ringing: cancel the dial (delete the `human-…` participant), end as a normal caller hang-up.

Participant handling:

- `_on_participant_disconnected` treats `human-{call_id}` leaving as the end of the call too (decision: person hangs up → call ends). Both legs leaving end the room.
- AMD stays bound to the caller identity only.
- `max_duration_seconds` stops applying once the contact answers. On answer the bridge handle cancels the pending soft cap task, so the call runs until the caller or the contact hangs up. Before the answer the cap applies as today: if it fires while the contact rings, the agent says the cap line, the room is deleted (both legs drop) and the job shuts down. A cap that already fired and is announcing when the contact answers finishes and ends the call.
- Stale-call sweep (`sweep_stale_calls`) and pool sweep (`sweep_pool_reservations`): a call with an `answered` `handover` event is not swept, and its pool number stays reserved, at `max_duration_seconds + grace`. A fixed 12 h backstop after `COALESCE(started_at, requested_at)` (`HANDOVER_BACKSTOP_SECONDS`) closes the row of a crashed worker. The voicebot does not enforce the 12 h; it never cuts a live call.
- Any job end while the contact leg is ringing or connected (soft cap, worker shutdown, BYO-LLM give-up) deletes the room, so no leg stays up unbilled.
- One dial at a time: a second `transfer_call` while one rings or is connected fails without dialing.
- Only one connected handover per call. After it, the tool returns "Already connected."

### 4. Billing

No change. `on_call_end` runs when the call ends, so duration covers the agent part and the human part.

- During a handover the carrier bills Hail for two legs, and LiveKit counts two SIP participants.
- The customer pays one flat 10¢/min (`hail-website/lib/private-rates.ts`).
- After the handover the agent is muted: no STT, LLM or TTS cost. That saving pays for the second leg.
- The destination guard (section 2) keeps the second leg inside countries Hail already sells numbers in.

### 5. Console (hail-website)

Built with the `frontend-design` skill.

- Agent page (`app/console/agents/AgentSheet.tsx`): new step "Hand over to a person".
  - Pick from contacts that have a phone. Search by name.
  - Each row: name, number, "when to use" note, reorder, remove.
  - Empty state links to Contacts.
  - Save errors from the API (DNC, no phone) shown on the row.
- Reads: `lib/agent-queries.ts` joins `agent_handover_contacts` + `contacts`.
- Writes: `app/console/agents/actions.ts` sends `handover_contacts`. Form state in `agent-form.ts`.
- Call drawer (`app/console/activity/ActivityDrawer.tsx`): `handover` event line, e.g. "Handed to Sam · answered after 12 s" or "Tried Sam · no answer".
- Tool listed in `lib/agent-tools.ts`.

### 6. Errors

| Case | What happens |
|---|---|
| Contact removed from agent during the call | Endpoint denies; agent says it cannot connect. |
| Contact on DNC / premium rate | Save fails (422). If it changes later, dial-time check denies. |
| Contact's country not in the call's carrier catalog | Endpoint denies; agent says it cannot connect. |
| No outbound trunk for the carrier | Endpoint denies and writes audit log `agent.handover.blocked` with reason `carrier_route_failed`; no `handover` event. |
| Person busy / no answer / declined | Agent comes back; event records SIP status. |
| Second handover attempt after connect | Tool returns "Already connected." |

### 7. Tests

- Core: tool spec + registry name set (`core/tests/test_agent_tools.py`); description never contains a number.
- API: agent create/update with handover contacts (wrong org, no phone, duplicate, >10, DNC, country not sold); internal endpoint (live call, removed link, DNC, country not in the call's carrier catalog, already connected); metadata carries targets without numbers.
- Voicebot: bridge answered (audio muted, event, webhook), no answer (agent resumes), person hangs up (call ends), caller hangs up while ringing (dial cancelled).
- Console: form state + validation (`__tests__/agent-form.test.ts`), event summary line.

## Non-goals

- SIP REFER (cold transfer).
- Handover for calls without a saved agent.
- Trying the next contact on no answer.
- Agent listening or resuming after the person joins.
- Showing the caller's number to the person.
- Separate billing for the second leg.
