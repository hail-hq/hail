# Inbound trunk lockdown

One LiveKit inbound trunk per carrier, so only the carrier can reach an
agent:

| Carrier | Lock                                                                        | Source                                                                                         |
| ------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| Twilio  | `allowed_addresses`: Twilio's regional SIP signaling ranges                 | [Twilio IP addresses](https://www.twilio.com/docs/sip-trunking/ip-addresses) (no machine file) |
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

Hail answers a call only on the trunk of the number's carrier ([`inbound_calls.py`](../../core/hailhq/core/inbound_calls.py)). While a carrier's key is empty, that is the shared trunk. Once the key is set and the services restart, that carrier's numbers still on the shared trunk are refused until `move` puts them on the carrier's trunk.

**So each carrier's inbound calls can fail for about 1 to 2 minutes**: from the restart until the last `move --yes` ends, test call included. Run each carrier at a quiet hour. The other carriers are not affected.

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

   2. Copy `.env` to the VM and restart as in step 5. The carrier's calls start failing now.
   3. At once, move the owner's numbers, then call one of them from a phone. The agent must answer. If it does not, roll back that carrier (below) and stop.

      ```bash
      uv run python scripts/ops/sip_trunks.py move --carrier twilio \
        --number +<owner's number> --yes
      ```

   4. Move the rest at once. The carrier's calls work again when this ends.

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

- One carrier, before `retire-shared`. **The carrier's calls can fail again for about 1 to 2 minutes**, from the restart until `move --back` ends.
  1. Empty its `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID=` in `.env`, copy it to the VM, and restart as in step 5.
  2. At once, move its numbers back. With the key empty, `move --back` finds the carrier trunk by its name in the JSON.

     ```bash
     uv run python scripts/ops/sip_trunks.py move --carrier twilio --back --yes
     ```

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
4. `source version changed` with no address change: read the page, then update `source.version` in a PR.
5. `could not fetch` or a new layout: fix the parser in [`check_sip_allowlist.py`](../../scripts/ops/check_sip_allowlist.py). Run it by hand with `uv run python scripts/ops/check_sip_allowlist.py` (the macOS python.org `python3` may fail with `CERTIFICATE_VERIFY_FAILED`).
6. Close the issue once **Actions → sip-allowlist-check → Run workflow** is green.
