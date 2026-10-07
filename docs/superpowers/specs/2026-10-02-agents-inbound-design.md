# Agents and inbound calls and texts

Status: approved by the owner on 2026-10-02 (mockup rev 2:
https://claude.ai/artifact/R5a5pyAR2xEwfKQZsy9uP3).

## Goal

A number can answer. An **agent** is a saved brain: instructions, greeting, AI
line, voice, tools, limits. A number routes its incoming calls to one agent and
its incoming texts to one agent. The same agent can place outbound calls through
`POST /calls` with `agent_id`.

Every carrier works the same way (Twilio, Telnyx, DIDWW, and any carrier added
later): Hail registers the number for inbound at the carrier and at LiveKit
when an agent is attached, and removes it when the agent is detached or the
number is released.

## Owner decisions

1. The AI line is spoken first on every inbound call, on by default, can be
   turned off per agent (same rule as outbound). Default inbound text:
   `Hi, this is an AI assistant answering on behalf of {org}.` The workspace
   default is edited in Call settings; an agent can override the text.
2. No credits: the call is not answered. Hail records the call as `failed`,
   `end_reason = insufficient_funds`, and sends `call.failed`.
3. Inbound minutes cost the same as outbound minutes (one voice rate).
4. Hail attaches numbers at the carrier for every carrier. No manual step.
5. Calls and texts ship together.
6. Settings live on the agent. The console is rebuilt around agents.

## LiveKit facts (docs fetched 2026-09-29)

- Inbound: LiveKit matches the INVITE to an inbound trunk by dialed number,
  then to a dispatch rule, creates the room and the SIP participant, and
  dispatches the agent named in `room_config.agents`. The agent gets the
  rule's static `metadata` string in `ctx.job.metadata`, so no per-call id
  can arrive that way.
- An inbound trunk with an empty `numbers` list needs auth or
  `allowed_addresses` (support enablement). Twilio Elastic SIP has no inbound
  auth. So each carrier gets one inbound trunk with an explicit `numbers`
  list, and Hail keeps that list current through
  `update_sip_inbound_trunk_fields(numbers=ListUpdate(add|remove))`.
- SIP participant attributes present at join: `sip.phoneNumber` (caller),
  `sip.trunkPhoneNumber` (dialed), `sip.trunkID`, `sip.callIDFull`,
  `sip.callStatus`.
- One dispatch rule, `dispatchRuleIndividual`, `roomPrefix: "hail-in-"`,
  `trunk_ids` = the carrier inbound trunks, `room_config.agents =
[{agent_name: "hail-voicebot", metadata: "{\"direction\":\"inbound\"}"}]`.
  Room names contain the caller number; `calls.from_e164` holds it anyway.
- Provider side: Twilio trunk origination URI `sip:<project>.sip.livekit.cloud;transport=tcp`
  and the number attached to the trunk; Telnyx FQDN connection with inbound
  enabled, `ani_number_format` and `dnis_number_format` set to `+E.164`, the
  FQDN = the LiveKit SIP endpoint; DIDWW voice IN trunk of type SIP pointing
  at the LiveKit SIP URI, assigned to the DID.

## Data model (migration 0050)

`agents`

- `id`, `organization_id`, `name` (text, 1..80)
- `system_prompt` (text, required), `first_message` (text, null = wait)
- `ai_disclosure` (bool, default true), `ai_disclosure_line` (text, null =
  workspace default)
- `voice_config` (jsonb, same shape as `VoiceConfig`), `tools` (text[] null =
  all), `max_duration_seconds` (int null = workspace default)
- `sms_enabled` (bool, default true): the agent answers texts on numbers that
  route texts to it
- `status` (`live` | `paused`), `created_at`, `updated_at`
- unique `(organization_id, name)`

`phone_numbers`

- `voice_agent_id` FK agents ON DELETE SET NULL
- `sms_agent_id` FK agents ON DELETE SET NULL
- `inbound_registered_at` (timestamptz null): set when the number is on the
  LiveKit inbound trunk and attached at the carrier

`calls`

- `from_number_id` becomes nullable; new `to_number_id` FK phone_numbers
- CHECK: `direction = 'outbound' AND from_number_id IS NOT NULL OR
direction = 'inbound' AND to_number_id IS NOT NULL`
- `agent_id` FK agents ON DELETE SET NULL
- `call_end_reason` enum gains `insufficient_funds`, `no_agent`

`sms`

- `agent_id` FK agents ON DELETE SET NULL (set on replies the agent wrote)
- `agent_reply_state` (text null): `pending` | `done` | `skipped` | `failed`,
  on inbound rows routed to an agent

`organization_call_settings`

- `max_duration_seconds` becomes nullable (null = service default)
- `ai_disclosure_line` (text null = built-in default). `{org}` is replaced by
  the workspace name.

## Settings and environment

New in `.env.example` (same commit as `config.py`):

```
LIVEKIT_SIP_INBOUND_TRUNK_ID=     # one inbound trunk for every carrier
TWILIO_SIP_TRUNK_SID=          # the Elastic SIP trunk numbers are attached to
DIDWW_VOICE_IN_TRUNK_ID=       # the DIDWW voice IN trunk pointing at LiveKit
```

`LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID` already exists. Telnyx reuses
`TELNYX_CONNECTION_ID` (the FQDN connection; the operator enables inbound on
it).

## Carrier registry

`carrier_routing.Carrier` gains:

- `inbound_trunk: Callable[[], str]`: the LiveKit inbound trunk id, raises
  `ValueError` when unset (same as `voice_route`).
- `attach_inbound: Callable[[PhoneNumber], Awaitable[None]]` and
  `detach_inbound`: carrier-side work.
  - Twilio: `trunking.v1.trunks(TWILIO_SIP_TRUNK_SID).phone_numbers.create(phone_number_sid=resource_id)`;
    detach deletes the association.
  - Telnyx: `PATCH /phone_numbers/{id}/voice {connection_id}`; detach clears it.
  - DIDWW: `PATCH /dids/{id}` with `relationships.voice_in_trunk` set to
    `DIDWW_VOICE_IN_TRUNK_ID`; detach sets it to null.

`inbound_routing.register(db, lk, number)` runs attach then LiveKit add, and
stamps `inbound_registered_at`. `unregister` does the reverse. A failure
leaves the number unregistered and returns 502 to the caller with the stage
name, like call setup.

A call is accepted only when its `sip.trunkID` equals the inbound trunk
configured for the number's carrier. LiveKit allows one wildcard inbound
trunk per project, so one setting, `LIVEKIT_SIP_INBOUND_TRUNK_ID`, serves
every carrier; `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID` overrides allow
separate trunks (with per-carrier `allowed_addresses`).

## API

Public (OpenAPI, then CLI codegen):

- `POST /agents`, `GET /agents`, `GET /agents/{id}`, `PATCH /agents/{id}`,
  `DELETE /agents/{id}` (detaches its numbers first).
- `PATCH /numbers/{id}` body `{voice_agent_id?, sms_agent_id?}` (null detaches).
  Setting `voice_agent_id` on an unregistered number registers it; clearing it
  unregisters. Release unregisters too.
- `PhoneNumberResponse` gains `voice_agent_id`, `sms_agent_id`,
  `inbound_registered` (bool).
- `POST /calls` gains optional `agent_id`. Explicit fields on the body win;
  the agent fills the rest. `system_prompt`/`llm` requirement is satisfied by
  an agent.
- `CallResponse` gains `agent_id`. `SmsResponse` gains `agent_id`.

Internal (HMAC):

- `GET /internal/orgs/{id}/call-settings` also returns `ai_disclosure_line`.
- `POST /internal/agent/reply-sms` `{sms_id, body}`: sends the agent's text
  reply. Runs suppression, funds, and billing like `/internal/agent/send-sms`.

## Inbound call path (voicebot)

1. `parse_metadata` accepts a payload without `call_id` when
   `direction == "inbound"`.
2. `entrypoint` inbound branch: `await ctx.wait_for_participant()`; require
   `PARTICIPANT_KIND_SIP`; read the `sip.*` attributes.
3. `core.inbound_calls.open_inbound_call(...)`:
   - number = active, non-pool `phone_numbers` row with `e164 = dialed`, with
     `voice` capability, `provider = carrier_for_inbound_trunk(trunk_id)`.
     Unknown number: drop (delete room, no row).
   - no `voice_agent_id`, or the agent is paused: insert `Call` failed,
     `end_reason = no_agent`, `call.failed`; delete room.
   - `has_funds` false, channel suspended, or org closed: insert `Call` failed,
     `end_reason = insufficient_funds` (or the existing suspension reason);
     `call.failed`; delete room.
   - else insert `Call(direction='inbound', status='ringing', to_number_id,
from_e164=caller, to_e164=dialed, agent_id, voice_config,
max_duration_seconds, provider, provider_call_sid=sip.callIDFull,
livekit_room=room, started_at=now, metadata={"billed": true})`, a
     `state_change` event `queued -> ringing`, and `call.received`.
   - returns the dispatch-shaped metadata dict (`call_id`, `organization_id`,
     `voice_config`, `system_prompt`, `first_message`, `ai_disclosure`,
     `ai_disclosure_line`, `tools`, `max_duration_seconds`, `org_name`,
     `direction`).
4. The rest of `entrypoint` is shared: session build, tools, soft cap,
   shutdown. AMD is skipped on inbound. `mark_call_answered` already accepts
   `ringing`. `on_call_end` is unchanged.
5. `disclosure_line(org_name, direction, template)`: template from the agent,
   else the workspace line, else the built-in default for the direction. With
   no org name the line is `Hi, this is an AI assistant.` plus the direction
   phrase without a name, as today.

## Inbound text path

1. `ingest_inbound_sms`: after the STOP/START/HELP handling, when
   `number.sms_agent_id` is set, the agent is live with `sms_enabled`, and the
   keyword action is `None`, set `agent_reply_state = 'pending'`.
2. The voicebot process runs a text worker (`hailhq.voicebot.textbot`) in a
   background thread with its own event loop, started in `main.py` before
   `cli.run_app`. It polls `sms` rows with `agent_reply_state = 'pending'`
   (`FOR UPDATE SKIP LOCKED`), builds the LLM with the existing
   `build_llm(None, org_llm)` (same precedence and keys as calls), and runs
   one `chat()` over: text preamble + agent instructions + the last 20
   messages between the two numbers in the last 24 hours.
3. The reply is sent through `POST /internal/agent/reply-sms`. Cap: 20 agent
   replies per thread per 24 hours, then `skipped`. No funds: `skipped`.
   LLM failure: `failed`, logged.
4. No tools on text replies in this release.

## Webhooks

- New `call.received` (inbound call arrived, before answer).
- Every `call.*` payload gains `direction`, `from`, `to`, `agent_id`.
- `sms.received` unchanged. Agent replies emit the existing outbound SMS
  events.

## Billing

- Voice: `on_call_end` writes `usage_events(channel='voice')` from
  `answered_at`, as today. The rater needs no change.
- Text replies bill as outbound SMS through the internal route.

## Console (hail-website)

- Sidebar group **Agents**: `/console/agents` (list), `/console/agents/new`,
  `/console/agents/[id]` (the sheet: Rings, Replies, Says AI line, Says
  greeting, Follows, Can, Sounds, Ends; rail: Try it with QR code, last 7 days,
  webhooks, API snippet).
- Numbers page: columns "Calls answered by" and "Texts answered by" with
  selects; carrier column.
- Call settings: AI line field above the duration limit; providers stay
  where they are and the agent sheet links to them.
- Activity: agent column; inbound rows; `call.received` in the trail.
- Copy: ASD-STE100, no em dash.

## Landing copy

Voice page, home products, pricing, about, compare, llms.txt, skill doc, legal
facts: remove "outbound only" and "inbound to follow"; add "answers calls and
texts".

## Docs (hail)

`docs/public/agents.md` (new), `docs/public/self-host/{twilio,telnyx,didww,livekit-cloud}.md`
inbound sections, `docs/public/architecture.md`, `docs/public/webhooks.md`,
`README.md` ticks, `CLAUDE.md` first line, `CHANGELOG.md`.

## Rollout

1. Merge hail PR; deploy runs migration 0050.
2. On the prod VM `.env`: the four new variables plus the LiveKit inbound trunk
   ids; recreate `api` and `voicebot`.
3. LiveKit Cloud: three inbound trunks (empty `numbers`, filled by Hail) and
   one dispatch rule as above.
4. Twilio: origination URI on the trunk. Telnyx: FQDN + inbound on the
   connection. DIDWW: voice IN trunk.
5. Merge hail-website PR.

## Out of scope

Agent tools on text replies; per-agent provider keys (providers stay
workspace-wide); call transfer; voicemail; LiveKit Phone Numbers.
