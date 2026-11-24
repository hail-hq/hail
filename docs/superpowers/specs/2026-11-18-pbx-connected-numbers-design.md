# Connect your phone system: a PBX sends calls to a Hail agent

## Goal

A business keeps its phone number, its phone provider and its phone system
(PBX). The PBX sends chosen calls to a Hail voice agent over a SIP trunk.

- The business does not port the number. Its provider and PBX stay as they are.
- The PBX decides which calls go to Hail: all calls, after hours, overflow, or an IVR option. Hail does not care which.
- The agent answers, can call out from the business's number, and can hand a call back to a person on a PBX extension.
- Setup is self-serve in the console and the API.

Why: many businesses run their numbers on a PBX (hosted or on-site) with a VoIP provider. Porting is slow and risky for them, and some providers only let a PBX sign in with a SIP username and password, which LiveKit cannot do. Their PBX can always add a "generic SIP trunk", so that is the one connection Hail needs to support first.

## Milestones

This design covers milestone 1. The others stay on the list and get their own design.

| # | Milestone | Status |
|---|---|---|
| 1 | Connect your phone system: calls in and out, handover to extensions | This design |
| 2 | Subscription or enterprise pricing for connected numbers, on the Stripe setup | Later, own session. Milestone 1 keeps prices in one config place so this can replace them. |
| 3 | Move your number to Hail (porting) into a Hail carrier | Later, own session. Research done for Italy: geographic numbers port to DIDWW in 7-14 days; Twilio ports them only from a fixed carrier list. |
| 4 | SIP server run by Hail (Asterisk): signs in to providers with SIP REGISTER, converts G.729. For customers with no PBX. | Later. Approach 2 below. |
| 5 | Texts on connected numbers: US and Canada landlines through hosted SMS (letter of authorization) | Later, only if customers ask. See "Texts". |

## Decisions (owner, 2026-11-18)

| Topic | Decision |
|---|---|
| Who keeps the line | The customer's PBX. Hail never signs in (SIP REGISTER) to the customer's provider in milestone 1. |
| Direction | Answer and call out. The agent calls out and hands over through the PBX. |
| How Hail accepts calls | One LiveKit inbound trunk and one outbound trunk per connection. |
| Inbound authentication | SIP digest (username and password Hail generates). Always on. |
| Handover | The agent dials the person through the PBX, as today's handover dials through a carrier. The agent comes back when nobody answers. |
| Caller ID on a handover | The original caller's number, when it is `+E.164` or a 1-6 digit extension. Otherwise (anonymous, unreadable) the connected number. |
| Price per minute | 10¢ a minute, every direction, every country. A handover adds 10¢ a minute for the handover leg, from its answer to its end, on top of the call (20¢ a minute while the person is on the line). Recording adds 1¢ a minute of call length (as today). Each price is one config value, easy to change. |
| Monthly and setup fee | $0 in milestone 1. Recurring pricing is milestone 2. |
| Ownership check | Hail calls the claimed number over the public phone network; the call must arrive back on the connection's trunk. Billed as a normal call. |
| SMS | None in milestone 1. |
| Porting | Milestone 3. |

## Approaches considered

1. **One LiveKit trunk pair per connection (chosen for milestone 1).** Hail creates the trunks through the LiveKit API. No server to run. Limits: LiveKit's codecs only (G.711 and G.722, no G.729), and no published limit on trunks per project.
2. **A SIP server run by Hail (Asterisk) in front of LiveKit (milestone 4, not rejected).** It can sign in to providers that need SIP REGISTER and convert G.729, so a customer with no PBX can connect a number. Costs: a VM to patch and watch, a server that can stop calls for every connected customer, and about 7 to 10 more days of work. It reuses all of milestone 1 (tables, routing, billing), so milestone 1 does not block it.
3. **One shared trunk for all connections.** Rejected: LiveKit allows one username and password per trunk, so customers would share credentials.

## Tested on 2026-11-18 (hosted PBX trial, owner's own numbers)

- PBX to Hail: a call from a PBX extension reached a Hail agent through a LiveKit inbound trunk. The agent answered in 2 s, the caller ID set on the PBX trunk arrived as `from_e164`, recording and transcript were saved.
- PBX to Hail, with password: a LiveKit inbound trunk with `auth_username`/`auth_password` challenged the PBX (`407`) and the PBX answered with the credentials. The call rang through. This is read from the ringing; LiveKit's own logs were not checked.
- Hail to PBX: `CreateSIPParticipant` through an outbound trunk whose address was the PBX host reached a PBX extension. The phone rang and was answered.
- The PBX sent the dialed number as `+E.164` in the Request-URI and To header, because its outbound rule added the `+`.
- This design relies on digest auth (`auth_username`/`auth_password`) on each connection trunk, which LiveKit always checks, not on IP lists.
- Calls at once: 2 agent-to-agent calls at the same time (4 agent sessions) all connected and ended normally. 6 calls at once is not tested yet (see "Order of work").
- Key presses (DTMF): not tested through the PBX yet.

## Today (verified in code)

- Numbers come only from carriers in `CARRIERS` (`core/hailhq/core/carrier_routing.py`). `voice_route(provider)` and `inbound_trunk(provider)` take a carrier name, not a number.
- Callers of `voice_route`: outbound calls (`api/hailhq/api/routes/calls.py`) and handover (`api/hailhq/api/routes/internal/agent.py`, `agent_handover`).
- Inbound: the voicebot reads the dialed number, `open_inbound_call` (`core/hailhq/core/inbound_calls.py`) finds the number with `active_dedicated_number` (`core/hailhq/core/sms_ingest.py`, `provisioning_state = 'active'` only) and refuses a call whose LiveKit trunk is not `inbound_trunk(number.provider)`.
- `inbound_routing.register` attaches the number at the carrier, then adds it to the LiveKit inbound trunk (`core/hailhq/core/livekit.py`, `add_inbound_number`).
- The dispatch rule `hail-inbound` lists its `trunk_ids`. A new trunk matches no rule until it is added.
- An inbound trunk with an empty `numbers` list accepts calls to any number. Only one such trunk may exist per project.
- Handover contacts are `contacts` rows with `phone_e164` (`agent_handover_contacts`). Handover requires a country Hail sells numbers in and passes `check_handover_allowed`. The contact sees the Hail number as caller ID today.
- Leg costs: `call_legs` with `CHECK provider IN ('twilio','telnyx','didww')`. The hail-website rater prices a missing leg from the rate table by provider.
- Secrets: `secret_cipher.py` (Fernet). Org BYO keys use `HAIL_PROVIDER_SECRET_KEY` (`provider_config.py`).
- The voicebot adds `+` to a digits-only number (`_e164` in `voicebot/hailhq/voicebot/agent.py`). An extension caller `101` becomes `+101`.
- DTMF: the agent can send key presses (`send_dtmf` tool) on any carrier. Hail does not read key presses from callers on any carrier today.

## Parity with carrier numbers

Full table with file references: kept with the build plan. Summary:

| Feature | Carrier numbers | Connected numbers |
|---|---|---|
| Agent answers inbound calls | Yes | Yes |
| Outbound calls (`POST /v1/calls`) | Yes | Yes, through the PBX |
| Handover to a person, cancel handover | Yes (E.164 contacts) | Yes: extensions and E.164 contacts, through the PBX |
| Answering-machine detection, voicemail check on handover | Yes | Yes (no carrier dependency). Risk: a PBX queue or IVR that answers at once looks like a pickup. |
| Agent sends key presses (`send_dtmf`) | Yes | Yes, to test per PBX |
| Agent reads caller key presses | No (not built) | No (same) |
| Recording, transcripts, `call.*` webhooks, max duration, balance cap | Yes | Yes |
| Leg costs from carrier records | Yes | Not needed: cost 0 |
| Carrier failure lookups (Twilio call record) | Twilio only | No |
| Search, quote, buy, renew, release at carrier, dunning, idle release | Yes | No: the number is the customer's. "Remove" replaces "Release". |
| Carrier registration (verification documents) | Where required | No: ownership check instead |
| SMS in and out, text agent, sender ID, US texting registration | Twilio, Telnyx | No (milestone 5) |

## Texts

- A SIP trunk does not carry texts. LiveKit's SIP server handles no SIP MESSAGE. Common PBXs send texts over HTTPS to the provider, not over the trunk.
- Only the carrier that holds a number can send texts from it. Hail cannot text from a number it does not hold at a carrier.
- Many landline numbers cannot text at all (for example Italian geographic numbers: no carrier Hail uses sells texting on them).
- Hosted SMS (texting turned on for a landline with a letter of authorization) exists for US and Canada numbers only.
- Milestone 1: the number is voice only. The agent can still text from a Hail number the workspace owns, and the console says the sender differs from the business number.

## Design

### 1. Data

New table `sip_connections`. Tables checked first: `org_provider_config` holds encrypted BYO keys but only for `llm`/`tts`/`stt` with one row per org, layer and provider, and the voicebot loads every row as pipeline settings; `phone_numbers` holds numbers, not credentials. Neither fits.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid NOT NULL | |
| `label` | text NOT NULL | "Main office PBX". 1-80 chars. |
| `inbound_username` | text NOT NULL UNIQUE | Hail generates it. |
| `encrypted_inbound_password` | text NOT NULL | Hail generates it. Fernet, `HAIL_PROVIDER_SECRET_KEY`. Text, not bytea: `SecretCipher` returns a `str` token, as `org_provider_config.encrypted_api_key` stores it. |
| `pbx_host` | text NULL | Host or IP Hail dials for outbound and handover. NULL: answer only. |
| `pbx_port` | int NOT NULL DEFAULT 5060 | |
| `pbx_transport` | text NOT NULL DEFAULT 'auto' | CHECK `IN ('auto','udp','tcp','tls')`. |
| `pbx_username` | text NULL | Optional (builder call, 2026-11-18): the tested PBX accepts trunk calls by host match only and has no setting to ask for a password. The setup page warns that LiveKit's source IPs are shared with other LiveKit customers, so the PBX must send trunk calls only to its own extensions, never back out to a carrier. |
| `encrypted_pbx_password` | text NULL | Same as above. |
| `default_region` | text NOT NULL | ISO country, to read national caller numbers. |
| `livekit_inbound_trunk_id` | text NULL | Set once created. |
| `livekit_outbound_trunk_id` | text NULL | NULL when `pbx_host` is NULL. |
| `created_at`, `updated_at`, `deleted_at` | timestamptz | |

`phone_numbers` changes:

- New provider value `pbx`. A connected number is a row with `provider = 'pbx'`, `capabilities = ['voice']`, `is_pool = false`.
- New `provisioning_state` value `unverified`. A `pbx` row starts `unverified` and becomes `active` after the ownership check. Not `pending`: the carrier order reconciler picks up `pending` rows. `active_dedicated_number` already ignores anything but `active`, so no agent answers an unverified number.
- `country_code` and `number_type` (both NOT NULL) come from `phonenumbers`: the number's region, and `local` for fixed lines, `mobile`, `toll_free`, or `national` for the rest.
- New column `sip_connection_id uuid NULL REFERENCES sip_connections(id) ON DELETE RESTRICT`. CHECK: `(provider = 'pbx') = (sip_connection_id IS NOT NULL)`.
- The live unique index on `e164` keeps one live owner per number. An `unverified` row counts, so it is removed after 7 days (section 5).

`call_legs`: widen the provider CHECK to include `pbx`.

`contacts`: new `extension text NULL` (section 6).

### 2. LiveKit trunks

New functions in `core/hailhq/core/livekit.py`: create, update and delete an inbound and an outbound trunk.

- Inbound trunk per connection:
  - `name`: `pbx-<connection id>`.
  - `numbers`: a fixed placeholder `pbx-<connection id>` (a SIP username form) plus each unverified or active number of the connection. The placeholder is never removed, so the list is never empty and the trunk can never become a wildcard.
  - `auth_username`, `auth_password`: the connection's inbound credentials.
- Outbound trunk per connection (only with `pbx_host`):
  - `address`: `pbx_host:pbx_port`, `transport`, optional `auth_username`/`auth_password`.
  - `numbers`: `["*"]`, so the handover leg can show the original caller's number as `sip_number`. The build plan probes this on a dev LiveKit project first. If LiveKit refuses `["*"]` or a `sip_number` not on the list, `numbers` is the connection's live numbers, and the handover caller ID falls back to the connected number.
  - The same probe checks that LiveKit accepts the `pbx-<connection id>` placeholder in an inbound trunk's `numbers`.
- Dispatch rule: one-time ops change. Set `hail-inbound`'s `trunk_ids` to empty, so it matches every trunk. Every trunk in the project is Hail's. Write it in `docs/public/self-host/livekit-cloud.md`.
- A connection's trunks are created with the connection and deleted with it (after its numbers are removed). Account close deletes them too (`api/hailhq/api/org_close.py`).

### 3. Routing by number, not by carrier

- `voice_route(provider)` becomes `voice_route(number: PhoneNumber)`. For `pbx` it returns the connection's `livekit_outbound_trunk_id` and no headers. It raises a clear error when the connection has no `pbx_host`.
- `inbound_trunk(provider)` becomes `inbound_trunk(number)`. For `pbx` it returns the connection's `livekit_inbound_trunk_id`.
- `pbx` is not added to `CARRIERS`. `CARRIERS` means "a carrier Hail buys numbers from". Code that loops over `CARRIERS` (quotes, orders, renewals) never sees `pbx`. Code that branches on membership (release, account close, `_sms_route_exists`) gets an explicit `pbx` branch.
- `inbound_routing.register`/`unregister`: for `pbx`, skip the carrier attach and detach; only add or remove the number on the connection's inbound trunk.
- Default caller ID: `resolve_org_number` (`api/hailhq/api/numbers.py`) picks the oldest active voice number when `from` is left out. It skips `pbx` numbers whose connection has no `pbx_host`.
- `PATCH /v1/numbers/{id}` refuses to route an `unverified` number to an agent.

### 4. Inbound lookup

- **Dialed number must be `+E.164` (builder call, 2026-11-18).** LiveKit picks the trunk by its `numbers` list before Hail sees the call, and only `+E.164` was tested. The setup page tells the customer to send the dialed number as `+E.164` (a PBX outbound rule that adds `+` and the country code). A call in another format matches no connection trunk and fails; the ownership check (5) shows this at setup.
- Look the number up within that connection only. Keep the trunk check.
- Caller number: normalize with `phonenumbers` and the connection's `default_region`. A short internal number (an extension calling in, 1-6 digits) stays as given, without a `+`. Anonymous or unparsable caller numbers stay as given.

### 5. Proving the customer owns the number

A test call the customer makes through their own PBX proves nothing: they choose what the PBX dials, and their SIP password can send an INVITE for any number.

**Decision (owner, 2026-11-18): Hail calls the number over the public phone network.**

- A new number on a connection starts `unverified`.
- The customer sets their PBX to send that number's incoming calls to Hail, then clicks "Check" (console) or calls `POST /v1/sip-connections/{id}/numbers/{number_id}/verify`.
- Hail places one outbound call from a Hail pool number, through a carrier, to the claimed number.
- The check passes when an INVITE for that number arrives on that connection's inbound trunk while Hail's outbound leg is ringing (30 s limit), **and** a 6-digit code that Hail sends as key presses on its outbound leg arrives on that inbound leg. The code only travels over the real phone network, so an INVITE the customer sends for a number they do not own cannot pass. Hail stores only a hash of the code. Only the first 6 digits received count, so a stream of key presses cannot try many codes; a digit pressed before the code fails the check, and the customer runs it again. Then Hail sets the number `active`, plays a short "connected" message, and hangs up both legs.
- It fails when the call arrives anywhere else, not at all, or without the code. Hail says why: no call arrived, arrived on the wrong trunk, or the code did not come through (the phone system drops key presses).
- Billed as a normal outbound call at the normal price for the destination. Max 5 checks per number per day.
- An `unverified` number older than 7 days is removed (frees the `e164`).
- The customer can change their PBX rule back (for example to after hours only) once checked.

### 6. Outbound calls and handover

- `POST /v1/calls` from a `pbx` number dials through the connection's outbound trunk. The PBX applies its own outbound rules and caller ID policy.
- **A contact may be an extension (builder call, 2026-11-18).** `contacts.extension`: 1-15 characters of digits, `*` or `#`. A ring group or queue ("Support line", ext 800) is a contact with only a name and an extension. Changes that follow:
  - `contacts_phone_or_email` CHECK becomes phone, email or extension. `ContactCreate`, MCP `create_contact`, CLI contacts and the console contact form accept it.
  - `validate_handover` and `handover_targets` (`core/hailhq/core/handover.py`), `_linked_person` and the phone check in `agent_handover` accept an extension contact on a `pbx` call. On a carrier call, an extension contact is skipped.
  - The sold-country check and `check_handover_allowed` do not apply to an extension (it never leaves the customer's PBX). They still apply to an E.164 contact dialed through the PBX.
  - The handover price quote: a `pbx` call quotes the `pbx` prices without a destination (10¢ a minute for the call, 20¢ a minute with the handover leg), so an extension never hits the E.164-only quote route.
- Caller ID on the handover leg: the original caller's number (decision), when it is `+E.164` or a 1-6 digit extension; an anonymous or unreadable caller gets the connected number. The PBX shows it to the person.
- While the handover rings: an email to contacts with an email, as today. No text (the number cannot text).

### 7. Billing

Prices (owner, 2026-11-18). Kept in `hail-website/lib/private-rates.ts` as one `pbx` entry (`PBX_PRICES`: `cents_per_minute`, `handover_cents_per_minute`, `monthly_cents`, `setup_cents`), so milestone 2 or another session can change them in one place.

| Item | Price |
|---|---|
| Call minute: inbound or outbound | 10¢ a minute of call length, every country |
| Handover leg on a connected call (owner, 2026-11-18) | +10¢ a minute, from the handover answer to its end, on top of the call minute. An unanswered dial costs nothing. |
| Recording | +1¢ a minute of call length, as today |
| Ownership check call | Normal outbound call price for the destination |
| Monthly fee per connected number | $0 |
| Setup fee | $0 |

How it is charged:

- Every leg on a `pbx` call is written to `call_legs` with `provider = 'pbx'`, `cost_status = 'found'`, `cost_usd = 0` at insert. The rater then charges the `pbx` per-minute price for the call, plus the handover price for each answered handover leg (its `answered_at` to `left_at` in `call_legs`, pro rata like the call).
- hail-website:
  - `voice-cost-rater.ts` and `voiceCallCents`: a `pbx` call uses the `pbx` price; a `pbx` call leg costs 0 and an answered `pbx` handover leg costs the handover price; neither is ever priced from the rate table.
  - Call price quote (`/api/internal/voice-rates/quote`): `pbx` quotes the `pbx` price, and the handover price on top for each handover leg, so hail holds enough balance while a person is on the line.
  - `monthly-fee-rater.ts`: skip `pbx` numbers. Without this, the run fails on a number with no price.
  - `idle-number-release.ts` and `number-dunning.ts`: skip `pbx` numbers.

What the customer sees:

- Console usage and call list: each call shows its number and "Your phone system" where carrier calls show nothing extra, its minutes and its charge.
- Console numbers list: the number with a "Connected" tag, the connection label, and its state (Checking / Connected). No price per month, no Release button; "Remove" instead.
- API: `GET /v1/calls/{id}` as today. `PhoneNumberResponse` gains `source` (`purchased` or `connected`), `sip_connection_id` and the `unverified` state. MCP `get_number` and `list_numbers` pass `source` through.
- Pricing page (hail-website): one line, "Connect your phone system: 10¢ a minute, no monthly fee". The copy ships after the rater, as the order-of-work rule says.

### 8. API, MCP, CLI, console

API (OpenAPI regenerated in the same PR):

```bash
# 1. Create a connection. The password is shown once.
curl -X POST https://api.hail.so/v1/sip-connections \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"label": "Main office", "default_region": "US",
       "pbx_host": "pbx.example.com"}'
# → {"id": "...", "sip_host": "<project>.sip.livekit.cloud",
#    "username": "hail-7f3a...", "password": "...", "state": "ready"}

# 2. Add the number.
curl -X POST https://api.hail.so/v1/sip-connections/<id>/numbers \
  -H "Authorization: Bearer $HAIL_API_KEY" -d '{"e164": "+14155550100"}'
# → {"id": "...", "e164": "+14155550100", "state": "unverified"}

# 3. After the PBX sends that number's calls to Hail: run the ownership check.
curl -X POST https://api.hail.so/v1/sip-connections/<id>/numbers/<number_id>/verify \
  -H "Authorization: Bearer $HAIL_API_KEY"
# → {"state": "active"} or {"state": "unverified", "reason": "no call arrived"}

# 4. Route it to an agent, as for any number.
curl -X PATCH https://api.hail.so/v1/numbers/<number_id> \
  -H "Authorization: Bearer $HAIL_API_KEY" -d '{"voice_agent_id": "<agent_id>"}'
```

Other routes: `GET /v1/sip-connections` and `GET /v1/sip-connections/{id}` (no passwords, last 4 characters only), `PATCH` (label, PBX host and credentials), `POST .../rotate-password`, `DELETE` (refused while it has numbers), `DELETE .../numbers/{number_id}` (removes the number and takes it off the trunk). `DELETE /v1/numbers/{id}` on a `pbx` number does the same remove.

MCP: `list_numbers`, `get_number` and `route_number` work for connected numbers, with `source`. No tool creates a connection or runs the check, because the connection returns a password and the check places a billed call. The agent tells the user to use the console.

CLI: generated from OpenAPI, plus:

```bash
hail sip-connections create --label "Main office" --region US --pbx-host pbx.example.com
hail sip-connections add-number <connection_id> +14155550100
hail sip-connections verify <connection_id> <number_id>
hail numbers update <number_id> --voice-agent <agent_id>
```

Console (hail-website, `app/console/numbers/`):

```
Numbers  ▸  [ Buy a number ]  [ Connect your phone system ]

Connect your phone system
 1. Name it            [ Main office          ]   Country [ US ▾ ]
 2. Add this SIP trunk to your phone system
      SIP server   <project>.sip.livekit.cloud   [copy]
      Username     hail-7f3a…                    [copy]
      Password     ••••••••  (shown once)        [copy]
      Audio        G.711 (PCMU or PCMA)
      Number format  +country code (for example +14155550100)
 3. Your number        [ +1 415 555 0100 ]
 4. Send this number's calls to Hail, then  [ Check ]
      ✓ Connected   or   ✗ No call arrived. Check step 2 and the routing rule.
 5. Calls out and handover (optional)
      Your phone system's address  [ pbx.example.com ]
 6. Choose the agent   [ Front desk ▾ ]
```

- The "Buy a number" / "Connect your phone system" choice sits at the top of the numbers page (owner's portal note, 2026-11-18). A customer can use both kinds of numbers in one workspace.
- Docs: `docs/public/connect-phone-system.md` (customer setup, the generic SIP trunk fields above, the calls-in-use note below) and an ops runbook in `docs/operations/`. Vendor-specific setup steps are fine in docs; marketing copy names no vendor.

## Risks and checks

- Codecs: the PBX trunk must offer PCMU or PCMA. The setup page says so. G.729 only is milestone 4.
- Calls at once: each connected call uses a LiveKit agent session and voicebot CPU like any call. LiveKit Agents stops taking new jobs above 70% CPU (`livekit/agents` default `load_threshold`). Test 6 calls at once before onboarding a customer who needs that many.
- The PBX's own limit: a call the PBX sends to Hail usually uses 2 of its call channels, and a handover adds 1. The setup page says so.
- LiveKit trunk count: no published limit. Ask LiveKit support before rollout to many customers.
- LiveKit project limits that every call shares (the self-hosted voicebot does not count against LiveKit's hosted agent session limit):
  - Recordings at once (egress): a recorded call holds one egress for its whole length. Past the limit, a new recording likely fails while the call continues (inferred from `core/hailhq/core/recordings.py`, not tested). Check the plan's egress limit before promising recording on many calls at once.
  - Adaptive interruption: a per-minute request limit and a concurrent connection limit. Past either limit, the agent falls back to VAD interruption (`livekit/agents/voice/agent_activity.py`, `_fallback_to_vad_interruption`); the call does not drop, but noise interrupts the agent more easily.
  - The 6-call test checks both, with recording on.
- DTMF: test the agent's key presses through the PBX at the first customer setup.
- A PBX queue or IVR that answers at once starts the call clock and answering-machine detection early. The setup page says to send calls straight to the trunk.
- A wrong PBX setting gives no call at all and Hail sees nothing. The ownership check catches it at setup.

## Order of work (builder call, 2026-11-18: no feature flag; the order keeps `pbx` rows from existing before billing knows them)

1. hail PR 1: migration, `sip_connections`, routing by number, LiveKit trunk functions, ownership check, zero-cost legs, extension contacts. No public route creates a `pbx` row yet.
2. hail-website PR 1: `pbx` price entry, rater, quote, monthly fee, idle release and dunning skips. Merges after hail PR 1 is deployed.
3. Ops: clear the dispatch rule's `trunk_ids`. Test 6 calls at once on one connection with voicebot CPU below 70% (needs enough LiveKit agent sessions on the project first).
4. hail PR 2: the public API, OpenAPI, CLI, MCP `source` field and docs. Merges after hail-website PR 1 is deployed (the exception in `CLAUDE.md` "Order of work": hail code that relies on a hail-website change ships after it).
5. hail-website PR 2: the console panel and the numbers list changes. Open to every workspace once merged.
6. hail-website PR 3: the pricing page line.
7. First customer: one joint test call each way, plus a key press and a handover.

## Plan 2 must

- Before any public route adds connected numbers, stop number squatting (owner decision, 2026-11-24): unchecked claims give way. When a Hail purchase or another workspace's connection wants a number held only by an unverified claim, Hail takes the number off that claim's trunk, releases the claim, then goes ahead. LiveKit lists a number on one inbound trunk only, so two claims never exist side by side. The cap of 10 unverified numbers per workspace stays.

## Left to the builder

- Exact names of new modules and functions.
- Where the "connected" message is played (voicebot path for the ownership check).
- Password length and username format (at least 24 random characters).
