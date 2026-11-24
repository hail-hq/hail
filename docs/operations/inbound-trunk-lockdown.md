# Inbound trunk lockdown

One LiveKit inbound trunk per carrier, so only the carrier can reach an
agent:

| Carrier | Lock                                                                        | Source                                                                                         |
| ------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Twilio  | `allowed_addresses`, plus the [secret header](#twilio-secret-header)        | [Twilio IP addresses](https://www.twilio.com/docs/sip-trunking/ip-addresses) (no machine file) |
| Telnyx  | `allowed_addresses`: each region's `primary`, `secondary`, `allowlistCidrs` | [voice.json](https://sip.telnyx.com/voice.json), `signaling` (not `uac`, that is SIP Attach)   |
| DIDWW   | digest `auth_username` / `auth_password`                                    | `DIDWW_SIP_INBOUND_PASSWORD`, set on both the LiveKit trunk and `DIDWW_VOICE_IN_TRUNK_ID`      |

- Settings: [`infra/livekit/inbound-trunks.json`](../../infra/livekit/inbound-trunks.json). No trunk ids, project URL or secrets: the repo is public.
- Apply and move: [`scripts/ops/sip_trunks.py`](../../scripts/ops/sip_trunks.py) (LiveKit API). Every command is a dry run until `--yes`.
- Weekly IP check: [`sip-allowlist-check`](../../.github/workflows/sip-allowlist-check.yml), Monday 09:30 UTC ([order](../../costs/README.md#weekly-jobs-in-order)).
- Negative test: [`scripts/ops/sip_invite_probe.py`](../../scripts/ops/sip_invite_probe.py).
- Exception to "infrastructure is Terraform only" (owner decision): LiveKit SIP trunks have no Terraform provider, so a committed JSON file plus this script define them.

How LiveKit picks a trunk ([`sip.go`](https://github.com/livekit/protocol/blob/main/sip/sip.go)):

- A number is listed on one trunk only. Two trunks listing it are refused.
- A trunk with no `numbers` matches every dialed number, and only one such trunk is allowed. So each trunk keeps a placeholder entry (`hail-placeholder-<carrier>`, a SIP user name no call uses). `move` adds `hail-placeholder-shared` to the shared trunk before it empties.
- A trunk whose `allowed_addresses` does not match the caller's address is skipped. LiveKit Cloud checks `allowed_addresses` only once it is enabled for the project ([docs](https://docs.livekit.io/telephony/accepting-calls/inbound-trunk/)). The negative test below proves whether it is.
- `auth_username`/`auth_password` is always checked (`407` without them).

## Rollout

Run every command from the repo root of the main checkout, whose `.env` holds the production values. Values marked _(chat)_ are not written here: the trunk ids, the LiveKit SIP host, the test numbers and the VM path.

Hail answers a call on the trunk of the number's carrier, and also on the shared trunk while `LIVEKIT_SIP_INBOUND_TRUNK_ID` is set ([`inbound_calls.py`](../../core/hailhq/core/inbound_calls.py)). So no call is refused while numbers move. LiveKit lists a number on one trunk only, so a moved number never arrives on the shared trunk.

Run `move` right after the restart anyway: a number routed to an agent between the restart and `move` can fail to register, because it is still listed on the shared trunk. Run `status` after the last `move` and move any leftover with `move --number`, or remove one that is no longer routed with `move --number <number> --drop-unknown`.

1. Merge the PR. The Deploy workflow builds and rolls `api`, `voicebot` and `mcp`. Wait until it is green. Merge changes no call behavior while the carrier keys are empty.
2. Update the checkout and look at the current state:

   ```bash
   git checkout main
   git pull
   uv sync --all-packages --all-extras
   uv run python scripts/ops/sip_trunks.py status
   ```

3. Create the three trunks. Run each without `--yes` first and read the plan:

   ```bash
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio --yes
   uv run python scripts/ops/sip_trunks.py apply --carrier telnyx
   uv run python scripts/ops/sip_trunks.py apply --carrier telnyx --yes
   uv run python scripts/ops/sip_trunks.py apply --carrier didww
   uv run python scripts/ops/sip_trunks.py apply --carrier didww --yes
   ```

   - `apply --carrier didww` also sets the digest user and password on `DIDWW_VOICE_IN_TRUNK_ID`. It reads that DIDWW trunk before any change, dry run too, and stops if it is not a SIP trunk.
   - Before any change it prints the DIDWW trunk's current `host`, `port`, `transport_protocol_id`, `auth_enabled` and `auth_user` (never the password). Keep that line _(chat)_: it is what to restore by hand.
   - Each `--yes` run prints `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID=ST_...` _(chat)_. Keep the three values for step 7. Do not put them in `.env` yet.

4. Right after `apply --carrier didww --yes`, call a Hail-owned DIDWW number _(chat)_ from a phone. The agent must answer. DIDWW numbers still use the shared trunk: this checks that the DIDWW trunk write did not break them. If it fails, restore the settings printed in step 3 at [my.didww.com](https://my.didww.com) → **Voice → Inbound Trunks** → the trunk, and stop.
5. The restart used in step 7 and in the rollback, on the VM after `.env` is copied there _(chat)_:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     up -d --no-deps --force-recreate api
   scripts/deploy/roll-voicebot.sh \
     docker compose -f docker-compose.yml -f docker-compose.prod.yml
   ```

6. Control run of the probe, before any number moves: point it at a Hail-owned number with an agent, still on the open shared trunk _(chat)_. The short call shows in that workspace's call log. It must print `ACCEPTED` (exit 1; it cancels the call at once). Any other verdict means the probe cannot see an open trunk from this network, so the negative probe in step 7 would prove nothing: fix that first.

   ```bash
   python3 scripts/ops/sip_invite_probe.py \
     --host <project>.sip.livekit.cloud \
     --number +<E.164 number on the shared trunk>
   ```

7. Move one carrier at a time, in this order: Twilio, Telnyx, DIDWW. The commands below show Twilio; use `telnyx` and `didww` for the others. For each carrier:
   1. Put only that carrier's `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID` from step 3 in `.env`. Read the plans now, before the window starts (they change nothing). Use the owner's own numbers of that carrier _(chat)_:

      ```bash
      uv run python scripts/ops/sip_trunks.py move --carrier twilio \
        --number +<owner's number>
      uv run python scripts/ops/sip_trunks.py move --carrier twilio
      ```

      `move` lists numbers on the shared trunk with no live row (released, failed or unknown to the database) and leaves them; add `--drop-unknown` to take them off, after checking the list.

   2. Copy `.env` to the VM and restart as in step 5. Calls keep working: numbers not moved yet still arrive on the shared trunk, which Hail accepts.
   3. At once, move the owner's numbers, then call one of them from a phone. The agent must answer. If it does not, roll back that carrier (below) and stop.

      ```bash
      uv run python scripts/ops/sip_trunks.py move --carrier twilio \
        --number +<owner's number> --yes
      ```

   4. Move the rest at once. Each number is on no trunk for one LiveKit request (well under a second) while it moves.

      ```bash
      uv run python scripts/ops/sip_trunks.py move --carrier twilio --yes
      uv run python scripts/ops/sip_trunks.py status
      ```

   5. Negative probe, from a laptop (not a carrier address), at the owner's number now on that carrier's trunk. Wait about a minute after the step 6 control run, or use another network: LiveKit may drop INVITEs from an address it takes for a flood, and the probe then prints `INCONCLUSIVE`.

      ```bash
      python3 scripts/ops/sip_invite_probe.py \
        --host <project>.sip.livekit.cloud \
        --number +<E.164 number on the trunk>
      ```

      - `REFUSED` (exit 0), only on a clear `401`, `403`, `404` or `407` answer: the trunk is locked. Twilio and Telnyx trunks answer `404` (no trunk matches the address) or `403`; DIDWW answers `401` or `407`. A mistyped number also gets `404`, so take the number from the `move` output.
      - `ACCEPTED` (exit 1): the trunk took a call from a non-carrier address. For Twilio or Telnyx this means LiveKit does not check `allowed_addresses` on this project, so those trunks are not locked. Leave the numbers where they are (no worse than before) and stop: the owner decides the next step.
      - `NO CONNECTION` or `INCONCLUSIVE` (exit 2): no connection to port 5060 over TCP, a dropped connection, no answer at all, or another answer (printed). The test says nothing; wait a minute and try again, or run it from another network.

8. When all three carriers pass, retire the shared trunk:

   ```bash
   uv run python scripts/ops/sip_trunks.py retire-shared
   uv run python scripts/ops/sip_trunks.py retire-shared --yes
   ```

   Empty `LIVEKIT_SIP_INBOUND_TRUNK_ID=` in `.env`, copy it to the VM, and restart as in step 5.

## Rollback

- One carrier, before `retire-shared`. Calls to moved numbers keep working: the shared trunk is accepted throughout. A number routed between steps 1 and 2 is refused after the restart until step 3, so run step 3 right after the restart. For Twilio with `HAIL_SIP_INBOUND_SECRET` set, do the [Secret header rollback](#secret-header-rollback) first: the shared trunk does not pass the header on, so Hail drops every call to a Twilio number on it (no call row). `move --carrier twilio --back` refuses while the key is set.
  1. Move its numbers back:

     ```bash
     uv run python scripts/ops/sip_trunks.py move --carrier twilio --back --yes
     ```

  2. Empty its `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID=` in `.env`, copy it to the VM, and restart as in step 5.
  3. Run the same `move --back` again, for numbers routed between steps 1 and 2. With the key empty it finds the carrier trunk by its name in the JSON.

  The carrier trunk can stay; it lists only its placeholder.

- After `retire-shared`: create an open shared trunk again ([LiveKit Cloud §4](../public/self-host/livekit-cloud.md#4-inbound-calls), with `"numbers": ["hail-placeholder-shared"]`), add it to the `hail-inbound` dispatch rule, and set `LIVEKIT_SIP_INBOUND_TRUNK_ID` in `.env`. Then do the one-carrier rollback above for each carrier.
- DIDWW credentials can stay on the DIDWW trunk. To remove them: [my.didww.com](https://my.didww.com) → **Voice → Inbound Trunks** → the trunk → **Authorization** off.

## When the weekly check fails

The issue "SIP allow-list: carrier IPs changed" lists what differs. Calls from a new carrier address are refused until it is on the trunk, so act the same day.

1. Read the carrier's page (links in the table above).
2. `published, not in our list (add)`: add the range to `allowed_addresses` in `infra/livekit/inbound-trunks.json`, and set `source.version` to the new version. Open a PR. After merge:

   ```bash
   git pull
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio --yes
   ```

3. `in our list, no longer published (review)`: nothing removes it automatically. Remove it in a PR only when the carrier confirms it is retired, then `apply` as above.
4. A `note: source version changed` line (no alert on its own): when convenient, update `source.version` in a PR.
5. `could not fetch` or a new layout: fix the parser in [`check_sip_allowlist.py`](../../scripts/ops/check_sip_allowlist.py). Run it by hand with `uv run python scripts/ops/check_sip_allowlist.py` (the macOS python.org `python3` may fail with `CERTIFICATE_VERIFY_FAILED`).
6. Close the issue once **Actions → sip-allowlist-check → Run workflow** is green.

## Twilio secret header

LiveKit Cloud checks `allowed_addresses` only once LiveKit enables that check for the project ([LiveKit Cloud §4](../public/self-host/livekit-cloud.md#4-inbound-calls)). Twilio cannot answer a digest challenge, so its calls carry a shared secret instead:

- Twilio: each origination URL of `TWILIO_SIP_TRUNK_SID` to a LiveKit SIP host (`*.sip.livekit.cloud`) ends in `?X-Hail-Secret=<HAIL_SIP_INBOUND_SECRET>`. Other URLs are left as they are (`skipped (not LiveKit)` in the plan). A skipped URL that reaches LiveKit by IP or CNAME sends no header, so Hail refuses its calls once the secret is on the VM: point it at the `*.sip.livekit.cloud` host. Twilio sends an `X-` header added to the origination URI on every call ([Origination URI X-headers](https://www.twilio.com/docs/sip-trunking#originationuri-x-headers)).
- LiveKit: the Twilio trunk's `headers_to_attributes` maps `X-Hail-Secret` to the participant attribute `hail.sip_secret` ([`SIPInboundTrunkInfo`](https://docs.livekit.io/reference/telephony/sip-api/#sipinboundtrunkinfo)).
- Hail: with `HAIL_SIP_INBOUND_SECRET` set, a call to a Twilio number is dropped unless the attribute equals the secret ([`inbound_calls.py`](../../core/hailhq/core/inbound_calls.py)). Empty = no check. The check applies to calls on the Twilio trunk only; each other carrier uses the lock in the table above.
  - This check runs before the trunk check. Only a call with a valid secret reaches the trunk check, which keeps a failed call row for real misrouting.
  - A refused call may be forged, on any trunk, so Hail writes no call row and sends no webhook. A failed row would let a forger put calls with any caller ID into a customer's call log.
  - It logs a warning `X-Hail-Secret header missing` or `wrong`. If the call arrived on the shared trunk, it also logs `Twilio number still on the shared trunk; move it` at INFO, once per number per voicebot process.
  - The value must be 20 or more letters, digits, `-` or `_`, and `LIVEKIT_TWILIO_SIP_INBOUND_TRUNK_ID` must name the Twilio trunk (not empty, not the shared trunk). Otherwise the check is off and the voicebot logs an ERROR (`HAIL_SIP_INBOUND_SECRET is set but no Twilio inbound trunk: secret check off`, or `is not 20 or more letters, digits, - or _`). No service stops at startup over it.
- The secret is not hidden inside LiveKit: anyone with access to the LiveKit project can read it as the `hail.sip_secret` participant attribute of a Twilio call. To rotate it: do the [Secret header rollback](#secret-header-rollback), then repeat [Secret header rollout](#secret-header-rollout) steps 3 to 7, with `--rotate` added to both `apply` commands in step 3.
- `apply --carrier twilio` sets the LiveKit map and the Twilio URLs. It skips both, with the plan line `secret header: skipped, <reason>`, when:
  - the key is empty, or not 20 or more letters, digits, `-` or `_`;
  - a live Twilio number is still on the shared trunk, routed for inbound calls or not (the plan lists them; run `move --carrier twilio`, or `move --carrier twilio --number <it> --drop-unknown` for one not routed, then `apply` again);
  - `TWILIO_SIP_TRUNK_SID` is empty or its origination URLs cannot be read;
  - no origination URL goes to LiveKit;
  - an origination URL already sends a different secret, and `--rotate` is not given.

  The allowed addresses are applied either way, so the weekly IP fix never waits for the secret.
- LiveKit's field update cannot set the map, so the first `apply` replaces the whole trunk with a copy read just before. Header maps other than `X-Hail-Secret` stay.
  - After the replace it reads the trunk again and adds back, one at a time, any number of the copy it lost (`re-add ...`). A number LiveKit refuses prints `could not re-add <number>` and stops with an error: add it by hand (`lk sip inbound update --numbers`).
  - If any other field differs from the copy (allowed addresses, digest user and the rest), it prints `the replace changed <field>: <before> -> <now>` and stops with an error: restore those fields by hand (`lk sip inbound update`), then run `apply` again.
  - A number routed in that same second is not in the copy and is lost: compare the number count in `status` before and after.
- Use `transport=tls` on the origination URI, so the header is encrypted between Twilio and LiveKit. `apply` keeps the transport it finds. With `transport=tcp` the header travels in plain text: change the URI in the Twilio console (**Elastic SIP Trunking → Manage → Trunks → the trunk → Origination**), then call a Twilio number to check.

### Secret header rollout

Run from the repo root of the main checkout. Values marked _(chat)_ are not written here.

1. Before you start: no Twilio number may be left on the shared trunk, because only the Twilio trunk maps the header. `uv run python scripts/ops/sip_trunks.py move --carrier twilio` must print `move 0 twilio number(s)`. Otherwise move them first (the [Rollout](#rollout) section above, step 7). Until they are moved, `apply` skips the header steps and lists the Twilio numbers left on the shared trunk, routed or not.
2. Merge the PR. The deploy changes no call: `HAIL_SIP_INBOUND_SECRET` stays empty in the main checkout `.env` and on the VM until step 3.
3. Generate the secret into the main checkout `.env`, then set both sides. The value is written here, and not before, so that an unrelated copy of `.env` to the VM cannot turn the check on before Twilio sends the header. The commands never print the value. Read the dry run first: the secret shows as `***`.

   ```bash
   git checkout main
   git pull
   uv sync --all-packages --all-extras
   grep -c '^HAIL_SIP_INBOUND_SECRET=$' .env
   SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
   sed -i '' -E "s/^HAIL_SIP_INBOUND_SECRET=.*/HAIL_SIP_INBOUND_SECRET=${SECRET}/" .env
   unset SECRET
   grep -c '^HAIL_SIP_INBOUND_SECRET=..*' .env
   uv run python scripts/ops/sip_trunks.py status
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio --yes
   uv run python scripts/ops/sip_trunks.py status
   ```

   Both `grep -c` lines must print `1` (empty before, set after). `sed -i ''` is the macOS form; on Linux use `sed -i`. The `apply` output must not contain `secret header: skipped`. The Twilio trunk line must say `secret header mapped yes`, with the same number count as before.

4. Call an owner Twilio number _(chat)_ from a phone. The agent answers: Hail does not check yet. The operator (agent) checks Logfire for `X-Hail-Secret header present` on that call (the voicebot log `inbound twilio call to +<number>: X-Hail-Secret header present`). If it says `missing`, LiveKit does not map the header: stop here.

5. Copy the main checkout `.env` to the VM `.env` _(chat)_. Then run the Deploy workflow, so `api` and `voicebot` start with the key, and wait until it is green:

   ```bash
   gh workflow run deploy.yml --ref main
   gh run list --workflow deploy.yml --limit 1
   ```

6. Call the same number again. The agent must answer.
7. Run the probe from a laptop at that number, with `--ring`. Without it, the probe hangs up at the first ring, before the voicebot checks the header, and proves nothing.

   ```bash
   python3 scripts/ops/sip_invite_probe.py \
     --ring \
     --host <project>.sip.livekit.cloud \
     --number +<E.164 number on the Twilio trunk>
   ```

   It must print `DROPPED:`, a ring (`180 Ringing` or another 18x), then a final code (exit 0). Example: `DROPPED: 180 Ringing, then 486 Rejected`. `DROPPED` alone proves nothing: LiveKit also ends a call nobody answers, after its ringing timeout, for example when the voicebot is down. The pass needs both:
   - Logfire shows the warning `inbound call to +<number> refused: X-Hail-Secret header missing (carrier=twilio)`;
   - the workspace's call log shows NO new call from `+12025550100`.

   `ACCEPTED: 200 OK (hung up)` (exit 1), or a new call row, means Hail did not drop the call: roll back ([Secret header rollback](#secret-header-rollback)). `INCONCLUSIVE` (exit 2) proves nothing: run it again. `REFUSED` (exit 0) means the trunk's IP lock refused the laptop before the voicebot saw the call: the header check cannot be tested from this address.

### Secret header rollback

Empty the value in the main checkout `.env`, copy it to the VM `.env`, and run the Deploy workflow as in [Secret header rollout](#secret-header-rollout) step 5. Twilio may keep sending the header; nothing reads it.

```bash
sed -i '' -E 's/^HAIL_SIP_INBOUND_SECRET=.*/HAIL_SIP_INBOUND_SECRET=/' .env
grep -c '^HAIL_SIP_INBOUND_SECRET=$' .env
```

To change the secret, roll back first, then repeat [Secret header rollout](#secret-header-rollout) steps 3 to 7 (step 3 generates the new value). Add `--rotate` to both `apply` commands in step 3: without it, `apply` keeps the old secret in the Twilio URLs. In the other order, every Twilio call is refused between `apply` and the end of the deploy.
