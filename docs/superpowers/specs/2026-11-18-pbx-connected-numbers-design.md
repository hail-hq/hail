# Connect your phone system: a PBX sends calls to a Hail agent

## Goal

A business keeps its phone number, its phone provider and its phone system
(PBX). The PBX sends chosen calls to a Hail voice agent over a SIP trunk.

- The business does not port the number. Its provider and PBX stay as they are.
- The PBX decides which calls go to Hail: all calls, after hours, overflow, or an IVR option. Hail does not care which.
- The agent answers, can call out from the business's number, and can hand a call back to a person on a PBX extension.
- Setup is self-serve in the console and the API.

Why: many businesses run their numbers on a PBX (hosted or on-site) with a VoIP provider. Porting is slow and risky for them, and some providers only let a PBX sign in with a SIP username and password, which LiveKit cannot do. Their PBX can always add a "generic SIP trunk", so that is the one connection Hail needs to support.

## Decisions (owner, 2026-11-18)

| Topic | Decision |
|---|---|
| Who keeps the line | The customer's PBX. Hail never signs in (SIP REGISTER) to the customer's provider. |
| Direction | Answer and call out. The agent calls out and hands over through the PBX. |
| How Hail accepts calls | One LiveKit inbound trunk and one outbound trunk per connection. No SIP server run by Hail. |
| Inbound authentication | SIP digest (username and password Hail generates). Always on. |
| Handover | The agent dials the person through the PBX, as today's handover dials through a carrier. The agent comes back when nobody answers. |
| Caller ID on a handover | The original caller's number. |
| Price per minute | The normal voice price. A connected-number call has no carrier cost, so the rater charges the per-minute minimum. |
| Monthly fee | None for a connected number. |
| SMS | None. Connected numbers are voice only. |
| Porting | Out of scope. A separate project. |
| Ownership check | Hail calls the claimed number over the public phone network; the call must arrive back on the connection's trunk. |

## Approaches considered

1. **One LiveKit trunk pair per connection (chosen).** Hail creates the trunks through the LiveKit API. No server to run. Limits: LiveKit's codecs only (G.711 and G.722, no G.729), and no published limit on trunks per project.
2. **A SIP server run by Hail (Asterisk) in front of LiveKit.** It could convert G.729 and sign in to providers that need SIP REGISTER. Rejected: a VM to patch and watch, one server that can take every customer down, and 7 to 10 more days of work. The PBX path does not need it.
3. **One shared trunk for all connections.** Rejected: LiveKit allows one username and password per trunk, so customers would share credentials.

## Tested on 2026-11-18 (hosted PBX trial, owner's own numbers)

- PBX to Hail, no password: a call from a PBX extension reached a Hail agent through today's shared inbound trunk. The agent answered in 2 s, the caller ID set on the PBX trunk arrived as `from_e164`, recording and transcript were saved.
- PBX to Hail, with password: a LiveKit inbound trunk with `auth_username`/`auth_password` challenged the PBX (`407`) and the PBX answered with the credentials. The call rang through. This is read from the ringing; LiveKit's own logs were not checked.
- Hail to PBX: `CreateSIPParticipant` through an outbound trunk whose address was the PBX host reached a PBX extension. The PBX matched the trunk by host, with no IP setup and no password. The phone rang and was answered.
- The PBX sent the dialed number as `+E.164` in the Request-URI and To header, because its outbound rule added the `+`.
- LiveKit `allowed_addresses` is not enforced on the project (a trunk listing only `192.0.2.1/32` still accepted calls). Digest auth is enforced. So this design uses digest auth, not IP lists.

## Today (verified in code)

- Numbers come only from carriers in `CARRIERS` (`core/hailhq/core/carrier_routing.py`). `voice_route(provider)` and `inbound_trunk(provider)` take a carrier name, not a number.
- Callers of `voice_route`: outbound calls (`api/hailhq/api/routes/calls.py`) and handover (`api/hailhq/api/routes/internal/agent.py`, `agent_handover`).
- Inbound: the voicebot reads the dialed number, `open_inbound_call` (`core/hailhq/core/inbound_calls.py`) finds the number and refuses a call whose LiveKit trunk is not `inbound_trunk(number.provider)`.
- `inbound_routing.register` attaches the number at the carrier, then adds it to the LiveKit inbound trunk (`core/hailhq/core/livekit.py`, `add_inbound_number`).
- The dispatch rule `hail-inbound` lists its `trunk_ids`. A new trunk matches no rule until it is added.
- An inbound trunk with an empty `numbers` list accepts calls to any number. Only one such trunk may exist per project.
- Handover contacts are `contacts` rows with `phone_e164` (`agent_handover_contacts`). Handover requires a country Hail sells numbers in and passes `check_handover_allowed`.
- Leg costs: `call_legs` with `CHECK provider IN ('twilio','telnyx','didww')`. The hail-website rater prices a missing leg from the rate table by provider.
- Secrets: `secret_cipher.py` (Fernet). Org BYO keys use `HAIL_PROVIDER_SECRET_KEY` (`provider_config.py`).
- The voicebot adds `+` to a digits-only dialed number (`_e164` in `voicebot/hailhq/voicebot/agent.py`). `0039...` would become `+0039...`.

## Design

### 1. Data

New table `sip_connections`. Tables checked first: `org_provider_config` holds encrypted BYO keys but only for `llm`/`tts`/`stt` with one row per org, layer and provider, and the voicebot loads every row as pipeline settings; `phone_numbers` holds numbers, not credentials. Neither fits.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `organization_id` | uuid NOT NULL | |
| `label` | text NOT NULL | "Main office PBX". 1-80 chars. |
| `inbound_username` | text NOT NULL UNIQUE | Hail generates it. |
| `encrypted_inbound_password` | bytea NOT NULL | Hail generates it. Fernet, `HAIL_PROVIDER_SECRET_KEY`. |
| `pbx_host` | text NULL | Host or IP Hail dials for outbound and handover. NULL: answer only. |
| `pbx_port` | int NOT NULL DEFAULT 5060 | |
| `pbx_transport` | text NOT NULL DEFAULT 'auto' | CHECK `IN ('auto','udp','tcp','tls')`. |
| `pbx_username` | text NULL | Only if the PBX asks Hail for a password on outbound. Optional (builder call, 2026-11-18): the tested PBX accepts trunk calls by host match only and has no way to ask for one. The setup page warns that LiveKit's source IPs are shared with other LiveKit customers, so the PBX must route trunk calls only to its own extensions, never back out to a carrier. |
| `encrypted_pbx_password` | bytea NULL | |
| `default_region` | text NOT NULL | ISO country, to read a national dialed number (`06...`). |
| `livekit_inbound_trunk_id` | text NULL | Set once created. |
| `livekit_outbound_trunk_id` | text NULL | NULL when `pbx_host` is NULL. |
| `created_at`, `updated_at`, `deleted_at` | timestamptz | |

`phone_numbers` changes:

- New provider value `pbx`. A connected number is a row with `provider = 'pbx'`, `capabilities = ['voice']`, `is_pool = false`, `provisioning_state = 'active'`.
- New column `sip_connection_id uuid NULL REFERENCES sip_connections(id) ON DELETE RESTRICT`.
- CHECK: `(provider = 'pbx') = (sip_connection_id IS NOT NULL)`.
- New column `pbx_verified_at timestamptz NULL` (see 5). Only `pbx` rows use it.
- The live unique index on `e164` already keeps one live owner per number.

`call_legs`: widen the provider CHECK to include `pbx`.

### 2. LiveKit trunks

New functions in `core/hailhq/core/livekit.py`: create, update and delete an inbound and an outbound trunk.

- Inbound trunk per connection:
  - `name`: `pbx-<connection id>`.
  - `numbers`: a fixed placeholder `pbx-<connection id>` (a SIP username form) plus each verified-or-pending number of the connection. The placeholder is never removed, so the list is never empty and the trunk can never become a wildcard.
  - `auth_username`, `auth_password`: the connection's inbound credentials.
- Outbound trunk per connection (only with `pbx_host`):
  - `address`: `pbx_host:pbx_port`, `transport`, optional `auth_username`/`auth_password`.
  - `numbers`: the connection's numbers (used as caller ID).
- Dispatch rule: one-time ops change. Set `hail-inbound`'s `trunk_ids` to empty, so it matches every trunk. Every trunk in the project is Hail's. Write it in `docs/public/self-host/livekit-cloud.md`.
- A connection's trunks are created when the connection is created, and deleted when it is deleted (after its numbers are removed).

### 3. Routing by number, not by carrier

- `voice_route(provider)` becomes `voice_route(number: PhoneNumber)`. For `pbx` it returns the connection's `livekit_outbound_trunk_id` and no headers. It raises a clear error when the connection has no `pbx_host`.
- `inbound_trunk(provider)` becomes `inbound_trunk(number)`. For `pbx` it returns the connection's `livekit_inbound_trunk_id`.
- `pbx` is not added to `CARRIERS`. `CARRIERS` means "a carrier Hail buys numbers from". Code that loops over `CARRIERS` (quotes, orders, renewals) never sees `pbx`. Code that branches on membership (`release`, account close, `_sms_route_exists`) gets an explicit `pbx` branch.
- `inbound_routing.register`/`unregister`: for `pbx`, skip the carrier attach and detach; only add or remove the number on the connection's inbound trunk.

### 4. Inbound lookup

- **Dialed number must be `+E.164` (builder call, 2026-11-18).** LiveKit picks the trunk by its `numbers` list before Hail sees the call, and only `+E.164` was tested. The setup page tells the customer to send the dialed number as `+E.164` (a PBX outbound rule that adds `+` and the country code). A call in another format matches no connection trunk and fails; the verification call (5) shows this at setup.
- Look the number up within that connection only. Keep the trunk check.
- Normalize the caller number with `phonenumbers` and the connection's `default_region`. Anonymous or unparsable caller numbers stay as given.

### 5. Proving the customer owns the number

A customer could claim a number that is not theirs, which would block the real owner at the `e164` unique index.

A test call the customer makes through their own PBX proves nothing: they choose what the PBX dials, and their SIP password can send an INVITE for any number.

**Decision (owner, 2026-11-18): Hail calls the number over the public phone network.**

- A new number on a connection starts unverified (`pbx_verified_at` NULL).
- The customer sets their PBX to send that number's incoming calls to Hail, then clicks "Verify" (console) or calls `POST /v1/sip-connections/{id}/numbers/{number_id}/verify`.
- Hail places one outbound call from a Hail pool number, through a carrier, to the claimed number. The call carries a one-time code in Hail's own records (call id), not in anything the customer controls.
- The call passes when an INVITE for that number arrives on that connection's inbound trunk within 30 s, while Hail's outbound leg is ringing. Hail then sets `pbx_verified_at`, answers with a short "connected" message, hangs up both legs.
- It fails when the call arrives anywhere else, or not at all. Hail shows why (no call arrived, or arrived without valid credentials).
- Cost: one short outbound leg from a pool number, billed to the workspace at the normal voice price (about one minute at most). Max 5 verify attempts per number per day.
- Until verified: no agent answers it, no outbound call or handover uses it.
- An unverified number older than 7 days is removed (frees the `e164`).
- The customer can change their PBX rule back (for example to after-hours only) once verified.

### 6. Outbound calls and handover

- `POST /v1/calls` from a `pbx` number dials through the connection's outbound trunk. The PBX applies its own outbound rules and caller ID policy.
- Handover through the PBX:
  - **A contact may be an extension (builder call, 2026-11-18).** Add `extension text NULL` to `contacts`, as a third allowed field next to `phone_e164` and `email`. A ring group or queue ("Support line", ext 800) is a contact with only a name and an extension. CHECK: an extension is 1-15 characters of digits, `*` or `#`. `agent_handover` accepts a contact with an extension when the call is on a `pbx` number.
  - On a `pbx` call, Hail dials the extension through the connection's outbound trunk. On a carrier call, an extension contact is skipped.
  - Caller ID on that leg: the original caller's number (decision). The PBX shows it to the person.
  - The sold-country check and `check_handover_allowed` do not apply to an extension (it never leaves the customer's PBX). They still apply to an E.164 contact dialed through the PBX.
- The text and email to the contact while it rings: unchanged for contacts with an email; no text (the number cannot send SMS).

### 7. Billing

- Every leg on a `pbx` call is written to `call_legs` with `provider = 'pbx'`, `cost_status = 'found'`, `cost_usd = 0` at insert. The rater then charges `max(minimum, agent fee + 0)`, which is the per-minute minimum.
- hail-website:
  - `voice-cost-rater.ts`: a `pbx` leg with no row is priced at 0, not from the rate table.
  - Call price quote (`/api/internal/voice-rates/quote`): `pbx` quotes the per-minute minimum only.
  - `monthly-fee-rater.ts`: skip `pbx` numbers (no monthly fee). Without this, the run fails on a number with no price.
  - `idle-number-release.ts` and `number-dunning.ts`: skip `pbx` numbers. Releasing them saves no carrier fee; removing them is the customer's choice.
- Our cost per connected minute is the AI part plus LiveKit's third-party SIP minute. The minimum covers it.

### 8. API, MCP, CLI, console

- API (OpenAPI regenerated in the same PR):
  - `POST /v1/sip-connections`: label, optional PBX host/port/transport/credentials, default region. Returns the SIP host, inbound username and inbound password. The password is shown once.
  - `GET /v1/sip-connections`, `GET /v1/sip-connections/{id}`: no passwords, last 4 characters only.
  - `PATCH /v1/sip-connections/{id}`: PBX host and credentials, label. `POST /v1/sip-connections/{id}/rotate-password`.
  - `DELETE /v1/sip-connections/{id}`: refused while it has numbers.
  - `POST /v1/sip-connections/{id}/numbers` with `e164`: adds an unverified `pbx` number. `DELETE` removes it.
  - `POST /v1/sip-connections/{id}/numbers/{number_id}/verify`: starts the ownership call (5). Returns the result when the call ends.
  - Existing `PATCH /v1/numbers/{id}` routes it to an agent once verified.
- MCP: `list_numbers`, `get_number`, `route_number` work unchanged. No tool creates a connection, because it returns a password.
- CLI: generated from OpenAPI, plus `hail sip-connections`.
- Console (hail-website, `app/console/numbers/`): a "Connect your phone system" panel beside buying a number. Steps: create the connection, copy the SIP host, username and password, add the number, make the test call, route it to an agent.
- Docs: `docs/public/connect-phone-system.md` (customer setup, generic SIP trunk fields, no vendor names in marketing copy; a vendor-specific setup page is fine in docs) and an ops runbook in `docs/operations/`.

## Non-goals

- Porting a number into Hail.
- Signing in to a provider with SIP REGISTER (needs approach 2).
- SMS on connected numbers.
- G.729 audio.
- Locking today's shared carrier trunk (the owner left it open on 2026-11-18).

## Risks and checks

- Codecs: the PBX trunk must offer PCMU or PCMA. The setup page says so.
- Calls at once: each connected call uses a LiveKit agent session and voicebot CPU like any call. LiveKit Agents stops taking jobs above 70% CPU. Run a load test before promising a customer a number of calls at once.
- The PBX's own limit: a call the PBX sends to Hail usually uses 2 of its call channels, and a handover adds 1. Say so in the setup page.
- LiveKit trunk count: no published limit. Ask LiveKit support before rollout to many customers.
- DTMF: LiveKit supports it; the method used by each PBX is not checked. Test key presses in the first customer setup.
- A wrong PBX setting gives no call at all and Hail sees nothing. The verification call (5) catches it at setup.

## Order of work (builder call, 2026-11-18: no feature flag; the order keeps `pbx` rows from existing before billing knows them)

1. hail PR 1: migration, `sip_connections`, routing by number, LiveKit trunk functions, verification, zero-cost legs. No public route creates a `pbx` row yet.
2. hail-website PR 1: rater, quote, monthly fee, idle release and dunning skips. Merges after hail PR 1 is deployed.
3. Ops: clear the dispatch rule's `trunk_ids`. Load test: 6 calls at once on one connection, with CPU on the voicebot server below 70%.
4. hail PR 2: the public API, OpenAPI, CLI and docs. Merges after hail-website PR 1 is deployed (the exception in `CLAUDE.md` "Order of work": hail code that relies on a hail-website change ships after it).
5. hail-website PR 2: the console panel. Open to every workspace once merged.
6. First customer: one joint test call each way, plus a key press and a handover.

## Left to the builder

- Exact names of new modules and functions.
- Where the verification "connected" message is played (voicebot path for unverified `pbx` numbers).
- Password length and username format (at least 24 random characters).
