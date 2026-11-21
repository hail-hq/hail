# US trial texting numbers

Trial texts (up to 25 per workspace, to its own verified phone) come from Hail pool numbers marked for trial texting. Add, swap, pause or remove one with the ops script. No deploy, no env var.

```bash
uv run python scripts/ops/sms_trial_numbers.py status
uv run python scripts/ops/sms_trial_numbers.py move --number +18005550199
uv run python scripts/ops/sms_trial_numbers.py move --number +18005550199 --yes
```

Run from the repo root of a checkout whose `.env` reaches the production database. Every change is a dry run until `--yes`. Code: [`scripts/ops/sms_trial_numbers.py`](../../scripts/ops/sms_trial_numbers.py), [`core/hailhq/core/sms_trial_pool.py`](../../core/hailhq/core/sms_trial_pool.py), [`core/hailhq/core/sms_trial.py`](../../core/hailhq/core/sms_trial.py).

## Commands

| Command  | What it does                                                                                                     |
| -------- | ---------------------------------------------------------------------------------------------------------------- |
| `status` | Each trial number: ready, not ready (and why) or paused, and which one sends now                                 |
| `mark`   | A call pool number becomes a trial number. It stops serving calls and browser sessions                           |
| `unmark` | A trial number goes back to the shared pool. Only a number that can make calls is ever claimed for a call        |
| `move`   | A workspace's number leaves the workspace and becomes a trial number (`organization_id` NULL, `is_pool`, marked) |
| `pause`  | Stops trial texts from the number at once. It stays a trial number, never back in the call pool                  |
| `resume` | Starts trial texts from a paused number again (once it is ready)                                                 |

`mark` and `move` take an active US toll-free number that can text, has a carrier id, and is on a carrier that confirms toll-free approval (Twilio only today). They refuse a number on a live call or browser session. `move` refuses a number an agent answers (remove the agent first) and a number on the texting registration of another number (only a registration that names the moved number is kept, see below). `move` clears `sms_registration_id` and keeps `messaging_service_sid` (the number keeps texting through its service). After `--yes`, both print the carrier's answer.

To stop all trial texts at once, `pause` the number that `status` shows as `sends now`, then any other ready one.

## The moved number's old registration

`move` leaves the workspace's toll-free registration row in place, still naming the number (`carrier_verifications.number_id`). Cancelling it would delete the toll-free verification at the carrier, so Hail keeps it:

- `DELETE /v1/verifications/{id}` answers 409 `This registration covers a Hail trial texting number and cannot be cancelled.`
- An account close of that workspace keeps the row and marks it `provider_refs.close_refused` with the number's id, like a refused release.

## How it works

- Marked: `phone_numbers.pool_purpose = 'sms_trial'` on a pool row. A CHECK allows it only on a toll-free pool number with no call reservation. `claim_pool_number` and browser messaging take only rows with `pool_purpose` NULL, and calls claim only numbers with `voice`.
- Ready: `provisioning_metadata.sms_trial_approved_at` is less than 24 hours old and `sms_trial_paused_at` is not set. The verification worker's pass (`HAIL_VERIFICATION_POLL_SECONDS`, default 600) asks the carrier for each marked number (`tollfree_approved`, skipping the 10 minute in-process cache) and saves the answer: yes stamps the time, no (also a 401, 403 or 404 from Twilio) removes it, a failed lookup keeps the last yes until it is 24 hours old. The worker logs trial counts only when an answer changes. With `HAIL_VERIFICATION_POLL_SECONDS=0` no pass runs, so every number stops being ready 24 hours after its last check.
- Requests never ask the carrier. `GET /v1/sms/trial`: `configured` = an active marked number exists, `available` = one is ready.
- Sender: the oldest ready number (`created_at`). Codes and trial texts come from it. Adding a newer number does not change it; unmarking or pausing it, or losing its approval, moves sends to the next ready one. A `from` that names another ready trial number sends from that number.
- Replies: STOP to a trial number opts the phone out (a suppression, `source = trial_stop_keyword`) in every workspace that holds it as its trial phone: verified, waiting for its code, or the new phone waiting beside a verified one. The consent check then refuses its trial texts, and a new code request answers 422 `This phone replied STOP to Hail. Reply START first.` START removes only those `trial_stop_keyword` rows; a STOP the phone sent to the workspace's own number stays. Every other text, and a keyword from a phone no workspace holds, is dropped: Hail does not read replies. Twilio also blocks texts to a phone that replied STOP ([error 21610](https://www.twilio.com/docs/api/errors/21610): per sender number or messaging service).

## Toll-free verification

File the toll-free verification for the number by hand in the Twilio console. Hail does not file it for pool numbers (they belong to no workspace). Once approved, run `status`: the number shows `ready` within one pass.
