# Carrier verification runbook

> **For developers and coding agents:** this page is self-contained. It says what carrier verification is, how a verification moves from created to approved, what runs in the background, what to configure, and where the code and tests are.

## What this is

Some countries require the buyer of a phone number to be verified first (ID, address, or a regulatory bundle). Hail collects the details in the console, files them with the carrier, and follows the carrier's decision. Once approved, `POST /numbers` can buy numbers of that country and type for the organization.

- Twilio is the only carrier plug-in in v1. It is registered in [`core/hailhq/core/providers/verification/__init__.py`](../../core/hailhq/core/providers/verification/__init__.py) and is active whenever `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set.
- Hail stores state, opaque carrier IDs, and the name of the person or company (`subject_name`). No addresses or documents are kept (table `carrier_verifications`, [`core/hailhq/core/models.py`](../../core/hailhq/core/models.py)). The name is never logged.
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

For a number (`purpose=number`): an organization may hold several live verifications per (carrier, country, number type, `subject_type`): several people, several companies. Only an exact duplicate is refused: the same `subject_type` and the same `subject_name`, ignoring case (unique index `carrier_verifications_number_live_uniq` on `(organization_id, provider, country_code, number_type, subject_type, lower(subject_name))`, rows with a name only). That is a 409 `You already have a verification for <name> as a person/company for this number type.` For US SMS (`purpose=sms`): one live registration per (organization, carrier, country, number type) (`carrier_verifications_sms_live_uniq`). `cancelled` and `rejected` rows do not count, so the customer can start over.

## Subject name

`POST /verifications` takes the name from the form the customer filled in. Each plug-in reads it with `subject_name` (no carrier call) before the draft is made; null when the form holds no name. `VerificationResponse.subject_name`, quote offers' `verifications[].subject_name` and `PhoneNumberResponse.subject_name` return it.

| Plug-in                                                    | Business                                                                                                              | Person                                     | Fallback                                                |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------- |
| Twilio number (`twilio.py`)                                | end-user field `business_name`                                                                                        | end-user fields `first_name` + `last_name` | same fields on a document, then `address.customer_name` |
| Telnyx number (`telnyx.py`)                                | `address.customer_name`; without an address, the text field whose label starts with "Company name" or "Business name" | `address.customer_name`                    | none (free-text contact fields are never read)          |
| DIDWW number (`didww.py`)                                  | identity field `company_name`                                                                                         | identity fields `first_name` + `last_name` | `address.customer_name`                                 |
| Twilio SMS (`twilio_sms.py`), Telnyx SMS (`telnyx_sms.py`) | `legal_name`                                                                                                          | `first_name` + `last_name`                 | `address.customer_name`                                 |

Names are trimmed and cut at 200 characters.

### Backfill for rows made before the name was kept

Rows created before `subject_name` existed have none. `python -m hailhq.core.verification_names` ([`core/hailhq/core/verification_names.py`](../../core/hailhq/core/verification_names.py)) reads the name back from the carrier for every row with `subject_name IS NULL`, through each plug-in's `fetch_subject_name`:

| Plug-in       | Carrier record read                                                                                                                      |
| ------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Twilio number | end user `refs.end_user_sid` (`business_name`, or `first_name` + `last_name`), else address `refs.address_sid` (`customer_name`)         |
| Telnyx number | address `refs.address_id` (`business_name`, or `first_name` + `last_name`); a group with no address keeps null                           |
| DIDWW number  | identity `refs.identity_id` (`company_name`, or `first_name` + `last_name`)                                                              |
| Twilio SMS    | customer profile `refs.profile_sid`: its business information (`business_name`) or starter profile (`first_name` + `last_name`) end user |
| Telnyx SMS    | brand `refs.brand_id` (`firstName` + `lastName`, else `companyName`)                                                                     |

It commits row by row and logs counts and row ids only, never a name. A carrier that is not configured, a record that is gone, or a lookup that fails leaves the row null; run it again later to retry. A live row whose name would duplicate another live row of the same kind stays null (counted as `duplicate`). Run it once after the deploy that adds the column, on the production VM:

```bash
cd /opt/hail
docker compose -f docker-compose.yml -f docker-compose.prod.yml \
  run --rm api python -m hailhq.core.verification_names
```

The last log line reads `verification names: N named, N without a name at the carrier, N carrier not configured, N lookup failed, N duplicate`.

## Flow

1. `GET /verifications/requirements?quote_id=<quote_id>&country_code=GB&number_type=mobile&subject_type=person` returns the form: fields, address rules, document slots. `subject_type` (`person` or `business`) is required on this route and on `POST /verifications` (422 without it). `quote_id` is the offer's id from `POST /numbers/quotes`. It is required for `purpose=number` (422 without it): Hail reads the carrier from it. A number support orders by hand also has an offer (`by_request: true`) with a `quote_id` for this.
2. `POST /verifications` (multipart, same `quote_id`) sends the details and files. The carrier evaluates them at once. Problems come back as 422 with the field named, and nothing is stored.
3. When the evaluation passes, the row is saved as `awaiting_review` and submitted to the carrier in the same request. There is no human gate. If the carrier is unreachable the row stays `awaiting_review`; the background pass retries. If the carrier refuses to take the draft, the row is `rejected` and the draft discarded (superadmin approve answers 409).
4. `GET /verifications/{id}` and `GET /verifications` ask the carrier for the current status at most once a minute per row and update `submitted` rows to `approved` or `rejected`.
5. Every `HAIL_VERIFICATION_POLL_SECONDS` the background pass (`sweep_verifications` in [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py)) does:
   - `awaiting_review` rows untouched for 2 minutes: lock the row (`FOR UPDATE SKIP LOCKED`), ask the carrier to check the draft. Pass → submit. Refused → `rejected` with the carrier's message, draft discarded, audit `verification.reject` by `system`. Carrier down → leave it; try next pass.
   - `submitting` and `submitted` rows: refresh from the carrier. A `submitting` row older than 5 minutes is settled from the carrier's status.
6. Quotes and purchases read the org's approved rows with `approved_handles` (a list per carrier, newest first, each with its `subject_type` and `subject_name`, carrier ids from each plug-in's `purchase_handle`). Hail's rows decide which approval is used. The carrier only confirms it is still valid, in its `offers` adapter: Twilio by bundle SID and the bundle's regulation (individual = person), Telnyx by requirement group id (kind-blind), DIDWW by address id. A carrier-side approval with no Hail row is not used. `POST /numbers/quotes` first refreshes the org's submitted rows for that country (at most once a minute per row).
7. Each offer lists `subject_types` (who the carrier accepts) and `verifications`: every live row for its carrier, country and type (`id`, `state`, `subject_type`, `subject_name`), approved first, then newest. `POST /numbers` takes `verification_id` (Hail's id): it must be one of the org's approved number rows that the carrier still accepts for this offer, else 422 on `verification_id`. Without it, the one approved valid row is used (after the optional `subject_type` filter); more than one is a 422 on `verification_id` (`You have more than one verification for this number. Pass verification_id.`). `subject_type` alone still gives `Verify as a person first` / `Verify as a company first` when that kind has no valid approval. The number stores `subject_type`, `subject_name` and `verification_row_id` in `provisioning_metadata.offer`; `PhoneNumberResponse` returns `subject_type`, `subject_name` and `verification_id` from it. A carrier rejection after the purchase (DIDWW) rejects that row only.

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
  -F 'fields={"legal_name":"Acme Inc","business_type":"PRIVATE_PROFIT","ein":"12-3456789","industry":"RETAIL","website":"https://acme.example","rep_name":"Ada Lovelace","rep_email":"ada@acme.example","rep_phone":"+15125550100","rep_title":"CEO","campaign_description":"Order updates for customers who bought on our site.","sample_1":"Acme: your order 123 shipped. Reply STOP to opt out.","sample_2":"Acme: your order 123 was delivered. Reply STOP to opt out.","opt_in_description":"Customers tick a separate, unchecked SMS box at checkout.","privacy_policy_url":"https://acme.example/privacy","terms_url":"https://acme.example/terms","has_links":"NO","text_types":"CUSTOMER_CARE,DELIVERY_NOTIFICATION"}' \
  -F 'address={"customer_name":"Acme Inc","street":"1 Main St","city":"Austin","region":"TX","postal_code":"78701","country_code":"US"}'
```

Same flow as above with `purpose=sms`: each organization registers its own US A2P 10DLC brand and campaign. The filing sends the customer's opt-in text followed by the fixed consent lines (not a condition of purchase, frequency, rates, STOP, HELP) and the privacy policy and terms links, because reviewers reject filings without them (`campaign_text` in [`sms_form.py`](../../core/hailhq/core/providers/verification/sms_form.py)). Design: [`docs/superpowers/specs/2026-10-13-us-sms-registration-design.md`](../superpowers/specs/2026-10-13-us-sms-registration-design.md). Fees: priced in the website (`lib/private-rates.ts`, private), charged per step by its US texting rater. CLI: `hail numbers register-sms`.

| State             | Meaning                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| `awaiting_review` | Profile and brand created at the carrier, not yet submitted.              |
| `submitting`      | Hail is submitting the brand.                                             |
| `submitted`       | Brand in vetting, or brand approved and campaign in carrier review.       |
| `approved`        | Campaign approved. Numbers attached. US texting works.                    |
| `rejected`        | Brand or campaign refused. `rejection_reason` carries the carrier's text. |
| `cancelled`       | Customer withdrew or dismissed.                                           |

- **Fees.** This repo holds no prices and takes no money for a registration. Hail Cloud's website rater charges each step at its private prices, keyed by the `carrier_verifications` row: a submission (any row, including a brand the carrier billed and then refused, saved as `rejected` with `provider_refs.refused_at_filing`), and a carrier review (the campaign id in `provider_refs`). The API pings it after each such commit (`notify_sms_registration_changed`, `POST /api/internal/sms-registrations/rate`); the rater is idempotent and also sweeps. Monthly fees are rated by the website too. The API only checks that the balance covers the carrier cost (`costs/sms-registration.json`) before filing (402). Rows filed before this change carry a single `sms_registration:<id>` debit and are not charged again.
- **Attach step.** After the campaign is approved, Hail attaches the organization's US long-code numbers to it, each on its own, and records each one in `phone_numbers.sms_registration_id`. The send gate lets a US-to-US text leave a long code only when that column points at an approved registration; `POST /sms` without `from` prefers such a number. A person's registration holds one number: on Twilio it sits in its own messaging service (`hail-org-<org>-person`) that holds only that number. The row is approved even when a number fails; the failure is logged with the number id and counted in the `verification.sms_attached` audit row (`failed`). `POST /numbers/{id}/enable-sms` attaches that number again. Until approval, texts from a US long code to a US number return 422.
- **Telnyx brand wait.** Telnyx registers a new 10DLC brand in the background: for about 2 seconds the brand id gets 404 from `smsOtp` and `vetting`, and 400 from `DELETE`. The draft reads the brand (up to 10 reads, 1 second apart) before it sends the PIN or asks for vetting. A refused brand (`failureReasons`) is a 422 on the form fields it names, and the brand is deleted; nothing is charged. A brand still registering after the wait is kept, and the background pass sends the PIN (`otp_sent_at` unset) or asks for vetting (`refs.vetting_requested` is `false`; a row without the key was filed before it existed and already asked). A read that fails with 429 or 5xx counts as "still registering". The background pass does nothing while the brand is `REGISTRATION_PENDING`, and rejects a `REGISTRATION_FAILED` brand.
- **Telnyx campaign.** The customer picks the kinds of texts they send (`text_types`, a `choices` field: a person 1 to 5, a company 2 to 5); they are filed as `subUsecases`, with `termsAndConditions: true`. A campaign drafted before the field existed is filed with `CUSTOMER_CARE` and `ACCOUNT_NOTIFICATION`. Only the Telnyx form asks for it: Twilio files no sub use cases. The verification's `step` (`confirm_identity`, `filing`, `carrier_review`) says where a filing in progress is. `SOLE_PROPRIETOR` needs at least 1 sub use case and `MIXED` at least 2 (`GET /10dlc/enum/usecase`); without them Telnyx answers 400. A refused campaign call leaves the row `submitted`; the background pass (every 10 minutes) files it again. The log line `10DLC status failed` holds Telnyx's error codes.
- **Telnyx STOP and HELP replies.** The campaign is filed with `optoutMessage` and `helpMessage`, each opening with the brand's name (else the name saved at filing, `subject_name` in refs); HELP gives the customer's own email (`contact_email` in the campaign snapshot, else hi@hail.so). Telnyx sends these replies itself from the messaging profile's auto-replies (`/messaging_profiles/{id}/autoresp_configs`, ops `stop` and `info`, country `US`), so the background pass sets them to the filed texts once the campaign exists (`replies_set` in refs). The texts live in `refs.replies`: kept at filing, taken from Telnyx's campaign record for a campaign adopted after a lost answer or a row filed before they were kept. Each reply is at most 255 characters: HELP falls back to hi@hail.so when the name and the email do not fit, then the name is cut at a word boundary. A filing with no profile (`messaging_profile_id` unset) finds or makes `hail-<org>` from `refs.organization_id`; a row without it logs and is skipped. A failure logs `10DLC auto-replies not set` and is tried again on the next pass. A campaign the carriers approved stays pending until the replies are set, for up to 6 polls; then it is approved and the log has the error `10DLC campaign approved without its STOP and HELP auto-replies` with the campaign, brand and profile ids. Set those replies by hand. Keep `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false`: Hail's own replies would text the subscriber twice.
- **Person confirm step.** A person's brand waits on the person. The row shows `confirmation`: `code` (Telnyx texts a PIN; the person enters it in Hail), `reply` (Twilio texts them; they answer that text), or `null`. A code goes to `POST /verifications/{id}/confirm`; on success Hail marks `identity_verified` in the refs and files the campaign at once. A wrong or expired code is 422 on `code`; anything else is 409. After 5 wrong codes the route returns 429 without calling the carrier, until a new code is sent (`refs.confirm_attempts` resets to 0). Telnyx sends a new PIN when the last one is over 24 hours old (on the next poll). The code is never stored or logged; the `verification.confirm` audit row holds `{"ok", "attempts"}`, never the code.

  ```bash
  curl -s -X POST "$HAIL_API_URL/verifications/$VERIFICATION_ID/confirm" \
    -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
    -d '{"code":"482913"}'
  ```

  CLI: `hail numbers confirm-sms <verification-id> --code 482913`.

- **`TWILIO_PRIMARY_PROFILE_SID`.** The approved Trust Hub primary profile (business identity ISV Reseller or Partner) that every Twilio brand is created under. Without it the Twilio SMS registration plug-in is off: `purpose=sms` with Twilio returns 404.

- **Owner emails.** When a verification becomes `approved` or `rejected` (carrier poll, superadmin reject, a draft the carrier refuses, a bought number whose registration is refused), the API pings the website after the commit (`notify_verification_decided`, `POST /api/internal/verifications/decided`). Session listeners in [`verification_decisions.py`](../../api/hailhq/api/verification_decisions.py) send the ping for any row whose `state` changed to one of those, so a new path needs no extra call. Polls lock the row, so two polls at once send one email. Hail Cloud's website emails the org's owners. A ping is fire-and-forget: if the website is down, that email is not sent. Self-hosted installs without the website send nothing.

## Cancel rules

`DELETE /verifications/{id}`:

- `awaiting_review` → `cancelled`, draft discarded at the carrier.
- `rejected` → `cancelled` (dismiss), draft discarded.
- `submitting`, `submitted` → **409** `this verification is under review; wait for the result`. The carrier keeps a draft it is reviewing, so cancelling would leave the customer's documents at the carrier with nothing pointing at them.
- `approved` with `purpose=sms` → `cancelled`. Each number on it (`phone_numbers.sms_registration_id`) leaves the campaign (Telnyx unlink; a person's Twilio number leaves the person's service and is set up in the org's service again), then the campaign, brand, bundle and profile are discarded. `cancelled_at` is set; the website rater bills the monthly fee through that month. Telnyx bills at least 3 monthly fees from approval, so for Telnyx the rater bills through the later of the cancel month and the approval month + 2 (`minimum_months` in `costs/sms-registration.json`). Audit row `verification.cancel`.
- `approved` with `purpose=number`, `cancelled` → 409.

## Superadmin routes

`/admin/verifications` (list, `POST /{id}/approve`, `POST /{id}/reject`) are not in the OpenAPI spec. Only a console session the website minted with `superadmin: true` passes ([`api/hailhq/api/superadmin.py`](../../api/hailhq/api/superadmin.py)); API keys never do. The console page lives in hail-website at `app/console/admin/verifications/`. Approve re-checks the draft with the carrier, then submits; reject records the reason and discards the draft. Both write an audit row (`verification.approve`, `verification.reject`) with the admin as actor.

## Where to look

- Routes and the background pass: [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py).
- Worker loop: [`api/hailhq/api/verification_worker.py`](../../api/hailhq/api/verification_worker.py).
- Carrier plug-in contract (`requirements`, `create_draft`, `check`, `submit`, `status`, `purchase_handle`, `discard`, `subject_name`, `fetch_subject_name`): [`core/hailhq/core/providers/verification/base.py`](../../core/hailhq/core/providers/verification/base.py). Twilio: [`twilio.py`](../../core/hailhq/core/providers/verification/twilio.py) next to it.
- Tests: [`api/tests/test_verifications_api.py`](../../api/tests/test_verifications_api.py) (`FakeCarrier` stands in for the carrier). Run: `cd api && uv run pytest tests/test_verifications_api.py -q`.
- Is the pass running? `docker compose logs api | grep "verification worker"`. The line appears only on a pass that changed something.
- Audit trail: `SELECT action, actor_kind, payload FROM audit_log WHERE target_type = 'carrier_verification' ORDER BY created_at DESC LIMIT 20;`

## Known gaps

- `VerificationResponse.provider` returns the carrier name to the customer. It predates the "never name a carrier" rule and has not been changed.
