# US trial texting numbers

Trial texts (up to 25 per workspace, to its own verified phone) come from Hail pool numbers marked for trial texting. Add, swap or remove one with the ops script. No deploy, no env var.

```bash
uv run python scripts/ops/sms_trial_numbers.py status
uv run python scripts/ops/sms_trial_numbers.py move --number +18005550199
uv run python scripts/ops/sms_trial_numbers.py move --number +18005550199 --yes
```

Run from the repo root of a checkout whose `.env` reaches the production database. Every change is a dry run until `--yes`. Code: [`scripts/ops/sms_trial_numbers.py`](../../scripts/ops/sms_trial_numbers.py), [`core/hailhq/core/sms_trial_pool.py`](../../core/hailhq/core/sms_trial_pool.py), [`core/hailhq/core/sms_trial.py`](../../core/hailhq/core/sms_trial.py).

## Commands

| Command  | What it does                                                                                                     |
| -------- | ---------------------------------------------------------------------------------------------------------------- |
| `status` | Each trial number, ready or not, and which one sends now                                                         |
| `mark`   | A call pool number becomes a trial number. It stops serving calls and browser sessions                           |
| `unmark` | A trial number goes back to the shared call pool (calls and browser sessions)                                    |
| `move`   | A workspace's number leaves the workspace and becomes a trial number (`organization_id` NULL, `is_pool`, marked) |

The number must be an active US toll-free number that can text and has a carrier id. `move` refuses a number an agent answers: remove the agent first. `move` clears `sms_registration_id` and keeps `messaging_service_sid` (the number keeps texting through its service).

**Do not cancel the old workspace's toll-free registration for a moved number.** A cancel deletes the toll-free verification at the carrier, and the number stops being ready. Closing that workspace cancels it too.

## How it works

- Marked: `phone_numbers.pool_purpose = 'sms_trial'` on a pool row. A CHECK allows it only on a toll-free pool number with no call reservation. `claim_pool_number` and browser messaging take only rows with `pool_purpose` NULL.
- Ready: `provisioning_metadata.sms_trial_approved_at` is set. The verification worker's pass (`HAIL_VERIFICATION_POLL_SECONDS`, default 600) asks the carrier for each marked number (`tollfree_approved`) and caches the answer: yes stamps the time, no removes it, a failed lookup keeps the last answer. `mark` and `move` with `--yes` ask once at once. With `HAIL_VERIFICATION_POLL_SECONDS=0` no pass runs, so an approval or a revocation is not seen until the script runs again.
- Requests never ask the carrier. `GET /v1/sms/trial`: `configured` = an active marked number exists, `available` = one is ready.
- Sender: the oldest ready number (`created_at`). Codes and trial texts come from it. Adding a newer number does not change it; unmarking it, or losing its approval, moves sends to the next ready one. A `from` that names another ready trial number sends from that number.
- Replies to a trial number are dropped (no workspace owns it). Twilio still blocks texts to a phone that replied STOP to that number (error 21610), and the code request then answers `This phone replied STOP to Hail`. A STOP covers only the number that got it.
- Only Twilio answers `tollfree_approved` today. A Telnyx or DIDWW toll-free number never becomes ready.

## Toll-free verification

File the toll-free verification for the number by hand in the Twilio console. Hail does not file it for pool numbers (they belong to no workspace). Once approved, run `status`: the number shows `ready` within one pass.
