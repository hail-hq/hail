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

1. Merge the PR. The Deploy workflow builds and rolls `api`, `voicebot` and `mcp`. Wait until it is green. From then on, a call on the shared trunk or on the carrier's own trunk is answered ([`inbound_calls.py`](../../core/hailhq/core/inbound_calls.py)).
2. Update the checkout and look at the current state:

   ```bash
   git checkout main
   git pull
   uv sync --all-packages --all-extras
   uv run python scripts/ops/sip_trunks.py status
   ```

3. Create the three trunks. Run each without `--yes` first and read the plan. `apply --carrier didww` also sets the digest user and password on `DIDWW_VOICE_IN_TRUNK_ID`; DIDWW sends them only when LiveKit asks.

   ```bash
   uv run python scripts/ops/sip_trunks.py apply --carrier twilio --yes
   uv run python scripts/ops/sip_trunks.py apply --carrier telnyx --yes
   uv run python scripts/ops/sip_trunks.py apply --carrier didww --yes
   ```

   Each prints `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID=ST_...` _(chat)_. Put the three values in `.env`.

4. Copy `.env` to the VM _(chat)_, then on the VM restart the two services that read it:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml \
     up -d --no-deps --force-recreate api
   scripts/deploy/roll-voicebot.sh \
     docker compose -f docker-compose.yml -f docker-compose.prod.yml
   ```

5. Move one carrier, test it, then the next. Telnyx first, then DIDWW, then Twilio:

   ```bash
   uv run python scripts/ops/sip_trunks.py move --carrier telnyx
   uv run python scripts/ops/sip_trunks.py move --carrier telnyx --yes
   uv run python scripts/ops/sip_trunks.py status
   ```

   Then call a Hail-owned number of that carrier _(chat)_ from a phone. The agent must answer. If it does not, roll back that carrier (below) before the next one.

6. Negative test, once per carrier, from a laptop (not a carrier address), at a Hail-owned number on that carrier's trunk _(chat)_:

   ```bash
   python3 scripts/ops/sip_invite_probe.py \
     --host <project>.sip.livekit.cloud \
     --number +<E.164 number on the trunk>
   ```

   - `REFUSED` (exit 0): the trunk is locked. Twilio and Telnyx trunks answer `404` (no trunk matches the address) or `403`; DIDWW answers `401` or `407`. A mistyped number also gets `404`, so take the number from the `move` output.
   - `ACCEPTED` (exit 1): the trunk took a call from a non-carrier address. For Twilio or Telnyx this means LiveKit does not check `allowed_addresses` on this project, so those trunks are not locked. Leave the numbers where they are (no worse than before) and stop: the owner decides the next step.
   - `NO CONNECTION` or `INCONCLUSIVE` (exit 2): no connection to port 5060 over TCP, a dropped connection, or another answer (printed). The test says nothing; try again, or from another network.

7. When all three carriers pass, retire the shared trunk and stop accepting it:

   ```bash
   uv run python scripts/ops/sip_trunks.py retire-shared
   uv run python scripts/ops/sip_trunks.py retire-shared --yes
   ```

   Empty `LIVEKIT_SIP_INBOUND_TRUNK_ID=` in `.env`, copy it to the VM, and repeat the restart in step 4. Then remove the temporary shared-trunk code (issue #475).

## Rollback

- One carrier, before `retire-shared`:

  ```bash
  uv run python scripts/ops/sip_trunks.py move --carrier telnyx --back --yes
  ```

  Then empty its `LIVEKIT_<CARRIER>_SIP_INBOUND_TRUNK_ID=` in `.env`, copy it to the VM, and restart as in step 4. The carrier trunk can stay; it lists only its placeholder.

- After `retire-shared`: create an open shared trunk again ([LiveKit Cloud §4](../public/self-host/livekit-cloud.md#4-inbound-calls), with `"numbers": ["hail-placeholder-shared"]`), add it to the `hail-inbound` dispatch rule, set `LIVEKIT_SIP_INBOUND_TRUNK_ID`, restart, then `move --back` and empty the carrier key as above.
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
5. `could not fetch` or a new layout: fix the parser in [`check_sip_allowlist.py`](../../scripts/ops/check_sip_allowlist.py). Run it by hand with `python3 scripts/ops/check_sip_allowlist.py`.
6. Close the issue once **Actions → sip-allowlist-check → Run workflow** is green.
