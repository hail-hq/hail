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
  --org <org uuid> --label "Main office" \
  [--region <phone system country, only when none of its numbers is from it>] [--pbx-host <host>] [--pbx-port 5060] [--pbx-transport auto]
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
- `no_call_arrived`: the PBX route, host, port or credentials are wrong, or the dialed number is not in +E.164 form. LiveKit refuses a call without valid digest auth before Hail sees it. A number not in +E.164 form is refused by LiveKit, or reaches Hail and is dropped as an unknown number (log line "inbound call to unknown/pool number … dropped"). Check the outbound route for this number and its + and country code, the PBX host and port, and the username and password.
- `not_authenticated`: the call reached Hail only on another trunk, never on the connection's own trunk before the check ended. For example, the PBX used another connection's username and password.
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

Before the first customer, place 6 calls at the same time on one connection. On a 3CX connection each agent call holds 2 of 3CX's simultaneous calls: the test needs a 3CX licence with at least 12, or place half the licence's count. Watch:

- Voicebot CPU stays below 70%.
- LiveKit project limits shared by every call. Past them a recording fails while the call continues, and interruption detection falls back to voice activity detection:
  - Recordings (egress) running at once stay below the project's limit.
  - Adaptive interruption requests per minute stay below the project's limit.
  - Adaptive interruption concurrent connections stay below the project's limit.
- Read the limits in the LiveKit Cloud console for the project.

## 8. 3CX

What a 3CX V20 admin enters, how to route a number to Hail, and how to read failures: [3cx-field-notes.md](3cx-field-notes.md).
