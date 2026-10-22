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

1. `GET /verifications/requirements?quote_id=<quote_id>&country_code=GB&number_type=mobile&subject_type=person` returns the form: fields, address rules, document slots. `quote_id` is the offer's id from `POST /numbers/quotes`. It is required for `purpose=number` (422 without it): Hail reads the carrier from it. A number support orders by hand also has an offer (`by_request: true`) with a `quote_id` for this.
2. `POST /verifications` (multipart, same `quote_id`) sends the details and files. The carrier evaluates them at once. Problems come back as 422 with the field named, and nothing is stored.
3. When the evaluation passes, the row is saved as `awaiting_review` and submitted to the carrier in the same request. There is no human gate. If the carrier is unreachable the row stays `awaiting_review`; the background pass retries.
4. `GET /verifications/{id}` and `GET /verifications` ask the carrier for the current status at most once a minute per row and update `submitted` rows to `approved` or `rejected`.
5. Every `HAIL_VERIFICATION_POLL_SECONDS` the background pass (`sweep_verifications` in [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py)) does:
   - `awaiting_review` rows untouched for 2 minutes: lock the row (`FOR UPDATE SKIP LOCKED`), ask the carrier to check the draft. Pass → submit. Refused → `rejected` with the carrier's message, draft discarded, audit `verification.reject` by `system`. Carrier down → leave it; try next pass.
   - `submitting` and `submitted` rows: refresh from the carrier. A `submitting` row older than 5 minutes is settled from the carrier's status.
6. When a number is bought, `approved_purchase_handle` hands the carrier's purchase values (for Twilio, the bundle SID) to the purchase call. No carrier call happens inside the purchase lock.

Runnable example:

```bash
curl -s -X POST "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY" \
  -F quote_id=<quote_id> -F country_code=GB -F number_type=mobile -F subject_type=person \
  -F 'fields={"first_name":"Ada","last_name":"Lovelace"}' \
  -F 'documents={"proof_of_identity":{"option":"passport"}}' \
  -F file.proof_of_identity=@passport.jpg
curl -s "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY"
```

## US SMS registration (`purpose=sms`)

Runnable example:

```bash
curl -s -X POST "$HAIL_API_URL/verifications" -H "Authorization: Bearer $HAIL_API_KEY" \
  -F purpose=sms -F country_code=US -F number_type=local -F subject_type=business -F provider=twilio \
  -F 'fields={"legal_name":"Acme Inc","business_type":"PRIVATE_PROFIT","ein":"12-3456789","industry":"RETAIL","website":"https://acme.example","rep_name":"Ada Lovelace","rep_email":"ada@acme.example","rep_phone":"+15125550100","rep_title":"CEO","campaign_description":"Order updates for customers who bought on our site.","sample_1":"Acme: your order 123 shipped. Reply STOP to opt out.","sample_2":"Acme: your order 123 was delivered. Reply STOP to opt out.","opt_in_description":"Customers tick a separate, unchecked SMS box at checkout.","privacy_policy_url":"https://acme.example/privacy","terms_url":"https://acme.example/terms","has_links":"NO"}' \
  -F 'address={"customer_name":"Acme Inc","street":"1 Main St","city":"Austin","region":"TX","postal_code":"78701","country_code":"US"}'
```

Same flow as above with `purpose=sms`: each organization registers its own US A2P 10DLC brand and campaign. The filing sends the customer's opt-in text followed by the fixed consent lines (not a condition of purchase, frequency, rates, STOP, HELP) and the privacy policy and terms links, because reviewers reject filings without them (`campaign_text` in [`sms_form.py`](../../core/hailhq/core/providers/verification/sms_form.py)). Design: [`docs/superpowers/specs/2026-10-13-us-sms-registration-design.md`](../superpowers/specs/2026-10-13-us-sms-registration-design.md). Fees: `GET /verifications/sms-fees`. CLI: `hail numbers register-sms`.

| State             | Meaning                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| `awaiting_review` | Profile and brand created at the carrier, not yet submitted.              |
| `submitting`      | Hail is submitting the brand.                                             |
| `submitted`       | Brand in vetting, or brand approved and campaign in carrier review.       |
| `approved`        | Campaign approved. Numbers attached. US texting works.                    |
| `rejected`        | Brand or campaign refused. `rejection_reason` carries the carrier's text. |
| `cancelled`       | Customer withdrew or dismissed.                                           |

- **Fee debit.** The carrier fee is debited from prepaid credit at filing. The ledger row in `account_credits` has `ref = sms_registration:<id>`. The shared self-hosted key (`HAIL_API_KEY`) is not debited.
- **Attach step.** After the campaign is approved, Hail attaches the organization's US long-code numbers to it, each on its own, and records each one in `phone_numbers.sms_registration_id`. The send gate lets a US-to-US text leave a long code only when that column points at an approved registration; `POST /sms` without `from` prefers such a number. A person's registration holds one number: on Twilio it sits in its own messaging service (`hail-org-<org>-person`) that holds only that number. The row is approved even when a number fails; the failure is logged with the number id and counted in the `verification.sms_attached` audit row (`failed`). `POST /numbers/{id}/enable-sms` attaches that number again. Until approval, texts from a US long code to a US number return 422.
- **Person confirm step.** A person's brand waits on the person. The row shows `confirmation`: `code` (Telnyx texts a PIN; the person enters it in Hail), `reply` (Twilio texts them; they answer that text), or `null`. A code goes to `POST /verifications/{id}/confirm`; on success Hail marks `identity_verified` in the refs and files the campaign at once. A wrong or expired code is 422 on `code`; anything else is 409. After 5 wrong codes the route returns 429 without calling the carrier, until a new code is sent (`refs.confirm_attempts` resets to 0). Telnyx sends a new PIN when the last one is over 24 hours old (on the next poll). The code is never stored or logged; the `verification.confirm` audit row holds `{"ok", "attempts"}`, never the code.

  ```bash
  curl -s -X POST "$HAIL_API_URL/verifications/$VERIFICATION_ID/confirm" \
    -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
    -d '{"code":"482913"}'
  ```

  CLI: `hail numbers confirm-sms <verification-id> --code 482913`.

- **`TWILIO_PRIMARY_PROFILE_SID`.** The approved Trust Hub primary profile (business identity ISV Reseller or Partner) that every Twilio brand is created under. Without it the Twilio SMS registration plug-in is off: `purpose=sms` with Twilio returns 404.

## Cancel rules

`DELETE /verifications/{id}`:

- `awaiting_review` → `cancelled`, draft discarded at the carrier.
- `rejected` → `cancelled` (dismiss), draft discarded.
- `submitting`, `submitted` → **409** `this verification is under review; wait for the result`. The carrier keeps a draft it is reviewing, so cancelling would leave the customer's documents at the carrier with nothing pointing at them.
- `approved` with `purpose=sms` → `cancelled`. Each number on it (`phone_numbers.sms_registration_id`) leaves the campaign (Telnyx unlink; a person's Twilio number leaves the person's service and is set up in the org's service again), then the campaign, brand, bundle and profile are discarded. `cancelled_at` is set; the website rater bills the monthly fee through that month. Telnyx bills at least 3 monthly fees from approval, so for Telnyx the rater bills through the later of the cancel month and the approval month + 2 (`minimum_months` in `costs/sms-registration.json`, also returned by `GET /verifications/sms-fees`). Audit row `verification.cancel`.
- `approved` with `purpose=number`, `cancelled` → 409.

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
