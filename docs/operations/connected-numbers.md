# Connected numbers

A customer's phone system (PBX) connects to Hail over a SIP trunk. Each connection has its own LiveKit inbound and outbound trunk. Customers set it up in the console or with [`/v1/sip-connections`](../public/connect-phone-system.md); the API needs `LIVEKIT_SIP_HOST` set (empty: the routes answer 503). The operator command below is for support.

## 1. Before any connected number exists

1. The hail-website change that adds the `pbx` price is deployed.
2. The dispatch rule `hail-inbound` lists no trunks. An empty list matches every inbound trunk in the project, so connection trunks need no edit. Set it from the main checkout, on the API host, with the env set:

   ```bash
   docker compose exec api python -m hailhq.api.connected_numbers_ops \
     dispatch-any --rule-name hail-inbound
   ```

   It prints the rule id and `trunks: none`. Exit 1 (`not_found`) means no rule has that name.
   - `lk sip dispatch update --trunks ""` does not work (lk 2.16.3: "unsupported list update operation"). Use the command above.
   - Verify: `lk sip dispatch list` shows `<any>` under SipTrunks for `hail-inbound`.
   - Never add a trunk to the rule after this. A rule that lists one trunk matches only the trunks it lists, and connection trunks stop being answered. `scripts/ops/sip_trunks.py apply` already skips the add on an empty list; skip it in the [inbound trunk runbook](inbound-trunk-lockdown.md) rollback too.

3. Do this before the first connection is created, and after the per-carrier trunks of the [inbound trunk runbook](inbound-trunk-lockdown.md) are applied.

Who can use it: every workspace, while `OPEN_TO_ALL_WORKSPACES` is `True` in `api/hailhq/api/routes/sip_connections.py` (`False`: staff console sessions only, everyone else gets 404), with the console feature `connectedPhoneSystems` in hail-website `lib/console-features.ts`. Change both together. A workspace sees only the first characters of the SIP server (`sip_host_masked: true`); staff console sessions and the shared key see it in full. Support sends the full address to the customer during setup.

## 2. Commands

Run on the API host, from the compose project directory:

```bash
docker compose exec api python -m hailhq.api.connected_numbers_ops create \
  --org <org uuid> --label "Main office" --region US \
  [--pbx-host <host>] [--pbx-port 5060] [--pbx-transport auto]
docker compose exec api python -m hailhq.api.connected_numbers_ops add \
  --connection <connection uuid> --number +14155550100
docker compose exec api python -m hailhq.api.connected_numbers_ops check \
  --number-id <number uuid>
docker compose exec api python -m hailhq.api.connected_numbers_ops remove \
  --number-id <number uuid>
docker compose exec api python -m hailhq.api.connected_numbers_ops delete \
  --connection <connection uuid>
docker compose exec api python -m hailhq.api.connected_numbers_ops release-claim \
  --number +14155550100
```

- `create` prints the connection id, the inbound username and the inbound password. Hail does not log it. Staff read it again in the console (Show), which writes `sip_connection.read_password` to the audit log.
- `add` prints the number id and its state (`unverified`).
- `check` prints the state, the reason and the check call id.
- `delete` works only when the connection has no numbers.
- `release-claim` frees a number held by an unchecked claim (section 5), for a number added by hand. It prints `released`, or exits 1 when the number has no unchecked claim.
- Exit codes: 0 done, 1 refused (prints the error `code` and `field`), 2 bad arguments.
- The command has no update or rotate: use `PATCH /v1/sip-connections/{id}` and `POST /v1/sip-connections/{id}/rotate-password` (or the console).

## 3. What the customer enters in their PBX

- SIP server: the LiveKit project's SIP URI (LiveKit Cloud console > Project settings).
- Username and password: from `create`.
- Codecs: PCMU or PCMA.
- DTMF: RFC 2833/4733 (key presses as telephone events). Without it the ownership check fails with `code_not_received`.
- Dialed number: `+E.164` (for example `+14155550100`), on every call to Hail.

## 4. Check results

The check calls the number from a Hail line and sends a 6-digit code as key presses.

- `active`: the call arrived on this connection's trunk with the right code.
- `no_call_arrived`: the PBX route, host, port or credentials are wrong. LiveKit refuses a call without valid digest auth before Hail sees it. Check the outbound route for this number, the PBX host and port, and the username and password.
- `not_authenticated`: the call reached Hail only on another trunk, never on the connection's own trunk before the check ended. Most likely the dialed number is not in +E.164 form.
- `code_not_received`: the call arrived but the code did not. The PBX drops key presses, or a digit was pressed before the code.

## 5. Limits

- PBX host: Hail refuses an IP address in a private, loopback, link-local or reserved range, and names such as `localhost` or ones ending in `.local` or `.internal`. Hail does not resolve the name. LiveKit's SIP service resolves it and dials from its own network (LiveKit Cloud's network, not Hail's). A self-hosted LiveKit SIP service must not be able to reach internal ranges: block its egress to them with a firewall.
- 5 checks per number per 24 hours.
- `unverified` numbers are removed after 7 days. One workspace holds at most 10 at a time (`too_many_unverified`).
- Unchecked claims give way. When a purchase (`POST /v1/numbers`) or another connection wants a number that is only `unverified` on a connection, Hail takes it off that connection's trunk, releases the claim (audit `number.claim_released` on the claim's workspace), then goes ahead. A claim whose check is running keeps the number for that check: the other side gets 409 "This number is being checked. Try again in 2 minutes." The same connection adding its own number again gets `number_taken`.
- The check call is billed as a normal outbound call.

## 6. Manual LiveKit probe

Run on 2026-11-18 with test trunks in the project, deleted afterwards:

- An inbound trunk with `numbers` set to the placeholder (`pbx-<connection id>`) plus `auth_username` and `auth_password` was accepted.
- An outbound trunk with `numbers` set to `["*"]` was accepted.
- `CreateSIPParticipant` with a `sip_number` that is on no trunk sent the INVITE to the PBX. LiveKit did not refuse the caller ID.

Re-run the probe if LiveKit changes how trunks or caller IDs work.

## 7. Six calls at once

Before the first customer, place 6 calls at the same time on one connection. Watch:

- Voicebot CPU stays below 70%.
- LiveKit project limits shared by every call. Past them a recording fails while the call continues, and interruption detection falls back to voice activity detection:
  - Recordings (egress) running at once stay below the project's limit.
  - Adaptive interruption requests per minute stay below the project's limit.
  - Adaptive interruption concurrent connections stay below the project's limit.
- Read the limits in the LiveKit Cloud console for the project.

## 8. 3CX V20 field notes

Menu names are from 3CX V20 Update 10.

**Trunk** (Admin > Voice & Chat > Add Trunk > Generic SIP Trunk):

- Registrar/Server: the SIP server from the connection, port 5060.
- Authentication ID and password: the connection's username and password. Seen working on one install with type of authentication `Do not require – IP based` (3CX still answered LiveKit's digest challenge); not checked in 3CX's docs. If calls never reach Hail, try a type that authenticates.
- Main Trunk Number is required. 3CX sends it as the caller on every call through the trunk (see caller ID below).
- DTMF: RFC 2833.

**Outbound rule** (Admin > Outbound Rules, move it to the top):

- Do not match the number itself. 3CX's E.164 setting ("remove the country code if same country") rewrites `+<cc>…` to the national form before rules run, so a `+<cc>` prefix never matches, and each number would need its own rule.
- Match a dial-prefix tag no other rule uses (for example `7799`). Route 1: the Hail trunk, strip the tag's digits, prepend `+<country code>`. One rule serves every number.
- Countries that drop the national trunk prefix in E.164 (the leading `0` in GB, DE and most others): strip the tag and that `0` too (5 digits for a 4-digit tag). Countries that keep it (IT): strip only the tag.
- "Calls from extension(s)", "Calls to numbers with a length of" and "Outbound Caller ID" stay empty. A length value makes the rule never match; an Outbound Caller ID replaces the real caller.

**Sending calls to the trunk**: forward to outside number `<tag><national number>`. Example in GB: the national `020 7946 0018` is forwarded to `779902079460018`; the rule strips 5 digits and prepends `+44`, so Hail receives `+442079460018`, the number on the connection.

- Testing: Admin > Advanced > CID Rules, one rule per caller (a staff mobile), on the trunk that receives the number, forward to `<tag><national number>`. Other callers keep reaching the business.
- The ownership check calls from a Hail pool line, often a different number on each check. Read the check call's number in 3CX's call history and match its start in the form 3CX shows it (with `+`, with `00`, or with neither). If no rule matches, 3CX answers the check itself (reception or menu) and the check ends `no_call_arrived`.
- Go-live: the number's inbound rule (office hours, out of office, or one menu option) forwards to `<tag><national number>`. Remove the test CID rules.

**Caller ID**: by default 3CX sends the Main Trunk Number as the caller, so every caller looks like the business's own number. Agents find earlier calls and texts by the caller's number, so callers' histories would mix. Fix it before go-live: V20 has no Outbound Parameters tab, so set `FromUserPart` to `$OriginatorCallerId` in a copy of the trunk's provider template (Advanced > Templates > SIP Trunk Providers) or in an exported trunk file, then import it. A test call from a mobile must show the mobile's number in Hail. Not yet checked in 3CX's own docs: the P-Asserted-Identity and Remote-Party-ID fields.

**3CX Event Log** (Admin > Event Log, errors and warnings only):

- `403` from another trunk's address: the call left on the business's normal line, so the Hail rule did not match (prefix, an empty field not empty, or rule order).
- `401` or `407` from the SIP server: wrong username or password.
- `486 Busy Here` from the SIP server: Hail refused the call (seen for a number with no agent routed). Read the failed call's end reason in Hail: `no_agent`, `insufficient_funds` and the like.
- No line at all and Hail says `no_call_arrived`: the CID or inbound rule did not catch the call.

**Agent voice**: set a voice native to the call's language; with no voice set, the default voice speaks with a foreign accent. Write brand names in the instructions and greeting as words ("Northgate"), not capitals ("NORTHGATE"): text-to-speech spells capitals letter by letter.
