# Carrier verification runbook

> **For developers and coding agents:** this page is self-contained. It says what carrier verification is, how a verification moves from created to approved, what runs in the background, what to configure, and where the code and tests are.

## What this is

Some countries require the buyer of a phone number to be verified first (ID, address, or a regulatory bundle). Hail collects the details in the console, files them with the carrier, and follows the carrier's decision. Once approved, `POST /numbers` can buy numbers of that country and type for the organization.

- Twilio is the only carrier plug-in in v1. It is registered in [`core/hailhq/core/providers/verification/__init__.py`](../../core/hailhq/core/providers/verification/__init__.py) and is active whenever `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set.
- Hail stores state and opaque carrier IDs only. No names, addresses, or documents are kept (table `carrier_verifications`, [`core/hailhq/core/models.py`](../../core/hailhq/core/models.py)).
- **Customer-facing text never names a carrier.** Error details, docstrings (they become OpenAPI descriptions), and console copy say "the carrier", never "Twilio" or "bundle".

## Configuration

| Variable                                  | Default                                                                     | Where                                                                    | Effect                                                                                                                               |
| ----------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `HAIL_VERIFICATION_POLL_SECONDS`          | `600` (in [`core/hailhq/core/config.py`](../../core/hailhq/core/config.py)) | `.env` on the host running `api` (`/opt/hail/.env` on the production VM) | Seconds between background passes. `0` disables the pass.                                                                            |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | empty                                                                       | same `.env`                                                              | Without both, no verification provider is configured and `POST /verifications` answers 404 `no verification provider is configured`. |

Nothing else needs to be set. A `.env` without `HAIL_VERIFICATION_POLL_SECONDS` runs the pass every 600 seconds because the code default applies. The pass starts inside the API process at boot ([`api/hailhq/api/main.py`](../../api/hailhq/api/main.py), `VerificationWorker`); there is no separate container.

## Lifecycle

| State             | Meaning                                                       | Who moves it here                                                                           |
| ----------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `awaiting_review` | Draft exists at the carrier, not yet sent for review.         | `POST /verifications`, or a submit that failed because the carrier was down.                |
| `submitting`      | Hail is sending it to the carrier right now.                  | `_submit_row` before the carrier call (so a crash cannot submit twice).                     |
| `submitted`       | Carrier is reviewing.                                         | `_submit_row` after the carrier accepted.                                                   |
| `approved`        | Carrier approved. Numbers can be bought.                      | `GET` refresh or the background pass, from the carrier's status.                            |
| `rejected`        | Carrier or a superadmin refused. `rejection_reason` says why. | Carrier status, superadmin reject, or the background pass when the carrier refuses a draft. |
| `cancelled`       | Customer withdrew or dismissed it.                            | `DELETE /verifications/{id}`.                                                               |

One live verification per (organization, carrier, country, number type). `cancelled` and `rejected` rows do not count, so the customer can start over.

## Flow

1. `GET /verifications/requirements?country_code=GB&number_type=mobile&subject_type=person` returns the form: fields, address rules, document slots.
2. `POST /verifications` (multipart) sends the details and files. The carrier evaluates them at once. Problems come back as 422 with the field named, and nothing is stored.
3. When the evaluation passes, the row is saved as `awaiting_review` and submitted to the carrier in the same request. There is no human gate. If the carrier is unreachable the row stays `awaiting_review`; the background pass retries.
4. `GET /verifications/{id}` and `GET /verifications` ask the carrier for the current status at most once a minute per row and update `submitted` rows to `approved` or `rejected`.
5. Every `HAIL_VERIFICATION_POLL_SECONDS` the background pass (`sweep_verifications` in [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py)) does:
   - `awaiting_review` rows untouched for 2 minutes: lock the row (`FOR UPDATE SKIP LOCKED`), ask the carrier to check the draft. Pass → submit. Refused → `rejected` with the carrier's message, draft discarded, audit `verification.reject` by `system`. Carrier down → leave it; try next pass.
   - `submitting` and `submitted` rows: refresh from the carrier. A `submitting` row older than 5 minutes is settled from the carrier's status.
6. When a number is bought, `approved_purchase_handle` hands the carrier's purchase values (for Twilio, the bundle SID) to the purchase call. No carrier call happens inside the purchase lock.

Runnable example:

```bash
curl -s -X POST "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY" \
  -F country_code=GB -F number_type=mobile -F subject_type=person \
  -F 'fields={"first_name":"Ada","last_name":"Lovelace"}' \
  -F 'documents={"proof_of_identity":{"option":"passport"}}' \
  -F file.proof_of_identity=@passport.jpg
curl -s "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY"
```

## Cancel rules

`DELETE /verifications/{id}`:

- `awaiting_review` → `cancelled`, draft discarded at the carrier.
- `rejected` → `cancelled` (dismiss), draft discarded.
- `submitting`, `submitted` → **409** `this verification is under review; wait for the result`. The carrier keeps a draft it is reviewing, so cancelling would leave the customer's documents at the carrier with nothing pointing at them.
- `approved`, `cancelled` → 409.

## Superadmin routes

`/admin/verifications` (list, `POST /{id}/approve`, `POST /{id}/reject`) are not in the OpenAPI spec. Only a console session the website minted with `superadmin: true` passes ([`api/hailhq/api/superadmin.py`](../../api/hailhq/api/superadmin.py)); API keys never do. The console page lives in hail-website at `app/console/admin/verifications/`. Approve re-checks the draft with the carrier, then submits; reject records the reason and discards the draft. Both write an audit row (`verification.approve`, `verification.reject`) with the admin as actor.

## Where to look

- Routes and the background pass: [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py).
- Worker loop: [`api/hailhq/api/verification_worker.py`](../../api/hailhq/api/verification_worker.py).
- Carrier plug-in contract (`requirements`, `create_draft`, `check`, `submit`, `status`, `purchase_handle`, `discard`): [`core/hailhq/core/providers/verification/base.py`](../../core/hailhq/core/providers/verification/base.py). Twilio: [`twilio.py`](../../core/hailhq/core/providers/verification/twilio.py) next to it.
- Tests: [`api/tests/test_verifications_api.py`](../../api/tests/test_verifications_api.py) (`FakeCarrier` stands in for the carrier). Run: `cd api && uv run pytest tests/test_verifications_api.py -q`.
- Is the pass running? `docker compose logs api | grep "verification worker"`. The line appears only on a pass that changed something.
- Audit trail: `SELECT action, actor_kind, payload FROM audit_log WHERE target_type = 'carrier_verification' ORDER BY created_at DESC LIMIT 20;`

## Known gaps

- `VerificationResponse.provider` returns the carrier name to the customer. It predates the "never name a carrier" rule and has not been changed.
