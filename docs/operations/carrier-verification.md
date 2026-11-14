# Carrier verification runbook

> **For developers and coding agents:** this page is self-contained. It says what carrier verification is, how a verification moves from created to approved, what runs in the background, what to configure, and where the code and tests are.

## What this is

Some countries require the buyer of a phone number to be verified first (ID, address, or a regulatory bundle). Hail collects the details in the console, files them with the carrier, and follows the carrier's decision. Once approved, `POST /numbers` can buy numbers of that country and type for the organization.

- Twilio is the only carrier plug-in in v1. It is registered in [`core/hailhq/core/providers/verification/__init__.py`](../../core/hailhq/core/providers/verification/__init__.py) and is active whenever `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set.
- Hail stores state, opaque carrier IDs, and the name of the person or company (`subject_name`). Number verification keeps no addresses or documents. SMS registration retains submitted text fields and address privately for application repair; document files are never kept on the verification (table `carrier_verifications`, [`core/hailhq/core/models.py`](../../core/hailhq/core/models.py)). A document sent with an upload link (`POST /v1/uploads`, then the `upload.<slot>` part) is stored until the verification is saved, then it is deleted. The GC (`core/hailhq/core/email_attachment_gc.py`, every 10 minutes) deletes any left 1 hour after upload. `POST /v1/verifications/check` runs every check before filing and saves nothing. The name is never logged.
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
   Credit hold (`purpose=number`, billed key, offer not `by_request`): the balance must cover the quote's `setup_cents + monthly_cents`, else 402 before any carrier call. It is checked again under `org_lock` when the row is saved; a late 402 discards the carrier draft. The same transaction writes a debit `verification_hold:<id>` (source `verification_hold`, channel `voice` or `sms` like a number order). A credit `verification_hold_return:<id>` (source `verification_hold_return`) gives it back when the row becomes `rejected` or `cancelled`, when `POST /numbers` buys with it (same commit as the order's `number_reservation` hold; both 402 checks count the open hold), or when the background pass finds it `approved` for 30 days (`approved_at`) with the hold still open. Helpers: [`core/hailhq/core/verification_hold.py`](../../core/hailhq/core/verification_hold.py). The ledger has no unique index on `ref`; a release locks the hold row, so it is written once. Quote offers show the open hold per verification as `verifications[].held_cents`. Verifications made before holds existed have none (`held_cents` 0).
3. When the evaluation passes, the row is saved as `awaiting_review` and submitted to the carrier in the same request. There is no human gate. If the carrier is unreachable the row stays `awaiting_review`; the background pass retries. If the carrier refuses to take the draft, the row is `rejected` and the draft discarded (superadmin approve answers 409). If the discard fails, the row keeps its ids and goes back to `awaiting_review` (approve still answers 409).
4. `GET /verifications/{id}` and `GET /verifications` ask the carrier for the current status at most once a minute per row and update `submitted` rows to `approved` or `rejected`.
5. Every `HAIL_VERIFICATION_POLL_SECONDS` the background pass (`sweep_verifications` in [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py)) does:
   - `awaiting_review` rows untouched for 2 minutes: lock the row (`FOR UPDATE SKIP LOCKED`), ask the carrier to check the draft. Pass → submit. Refused → `rejected` with the carrier's message, draft discarded, audit `verification.reject` by `system`. Before the discard, ids the carrier call wrote are committed, and the row is locked again (waiting, not skipping); a row that changed meanwhile is left as it is. Discard fails → the row keeps its ids and its state. Carrier down → the ids it wrote are saved; try next pass. A check, submit, status poll, or discard (of a refused draft or a superadmin reject) that fails partway keeps only the ids it added (new keys, empty values filled in, items appended to a list, the same inside nested objects); keys it removed or changed go back to what they were. A customer cancel's discard is not covered: it fails as a whole.
   - `submitting` and `submitted` rows: refresh from the carrier. A `submitting` row older than 5 minutes is settled from the carrier's status. A poll that fails keeps only the ids it added, as above.
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

US local numbers and local numbers in AS, GU, MP, PR and VI share this registration.
Keep `country_code=US` on the campaign request and pass the local number's `number_id`.
The number keeps its actual country code. A business campaign covers the organization's
local texting numbers on that carrier. A person's campaign still holds only one number.
Texts between these domestic destinations require approval before sending. Buying a number
without identity documents does not remove the separate texting registration requirement.
The verification worker also repairs missing links to already-approved campaigns. It retries
carrier failures and preserves the one-number limit for a person. It does not file new campaigns.
A repair writes a `verification.sms_attached` audit row only when a number attached. The first
refusal of a number writes a `verification.sms_attach_refused` audit row, and so does each later
refusal with a new carrier reason (see Attach step).

Fetch current requirements before constructing a submission. The shared form now collects
sender eligibility, the public program name, one to five actual purposes, representative
samples with purpose mappings, and every actual consent method. Each selected method needs
its own evidence. Website, private-form, paper, verbal, keyword, QR and inbound journeys have
different required fields. Evidence must be public and redacted; private recipient records
must not be uploaded as evidence. URL syntax checks do not establish reachability or approval.

Carriers take evidence only as a public link inside the campaign text; the Telnyx campaign API
has no upload field. A customer with no link uploads a PNG, JPG or PDF (10 MB or less) with
`POST /v1/verifications/sms/evidence` and puts the returned `url` in the `*_evidence_url` field.
Hail keeps the file in the mail bucket under `sms-evidence/<organization_id>/<token>` and `GET /v1/verifications/sms/evidence/<organization_id>/<token>` (no sign-in) redirects to a
storage link that works for 10 minutes. The API never streams the file. The
32-character random token is the only credential. The type comes from the file's first bytes;
nothing else is stored or served. Files stay while the account is open, because reviewers can
open the link again on a resubmission. The retention sweep (`python -m hailhq.core.retention`)
deletes an organization's folder 12 months after the account closes. Uploads need a mail
bucket and a public https `HAIL_API_URL`; without them the route returns 503 and the customer
pastes a public link. The public link is exempt from the general rate limit, like unsubscribe.
`DELETE /v1/verifications/sms/evidence/<organization_id>/<token>` (signed in, own organization
only) removes a file at once; its link then leads to a storage error page. An organization keeps at most 100 files; the 101st upload returns
409 until one is deleted.
Verbal consent needs the script and action only; its evidence link is optional.

The application compiler uses the applicant's actual disclosures, recording process, evidence
and policy links. It never pads a short answer with a consent assertion. Short answers and
inconsistent content flags return field errors before a paid filing. Content flags and
consent attestations are trimmed consistently when validating and compiling the filing. Fix the real enrollment
flow before confirming its disclosures. Unsupported or restricted programs require support
review. Sole proprietor eligibility is distinct from an account's individual/company label.

SMS answers survive rejection and cancellation. Retrieve them through the authenticated,
organization-scoped `GET /verifications/{id}/application` endpoint; its response is `no-store`.
The ordinary verification response never includes the application. For an older application,
the API may recover the original sender fields through a read-only provider lookup. If they
cannot be recovered, a 404 asks for the actual original details; do not guess them. A provider
outage returns a retryable error. A saved application is not proof of recipient consent.
The background pass deletes the saved answers (`provider_refs.application`) of a cancelled or
rejected registration 30 days after it ended (`cancelled_at`, else `updated_at`;
`APPLICATION_KEPT_FOR` in `routes/verifications.py`). The rest of the refs stay for billing.
After that, `GET .../application` returns 404 and does not rebuild the answers from the carrier.

A legacy brand-only registration that lacks current campaign evidence stays `submitted` with
`step=needs_information`. It retains the same registration, sender identity, paid brand and
provider IDs. It is not a carrier rejection and must not be cancelled and recreated to repair
its evidence. Already-filed campaigns continue normal review, and approved registrations
remain approved; this evidence gate applies before a new campaign is filed.

A campaign create the carrier refuses for an answer the customer gave gets the same hold, on
Telnyx and Twilio, for a person or a company: `provider_refs.needs_information` and
`campaign_needs_repair` are set, the paid brand stays, and `rejection_reason` holds Hail's
repair guidance. The carrier's own text goes only to the audit row
`verification.campaign_refused` (it can quote personal data). Before this, the row was
`rejected` and dismissing it deleted a company's paid brand.

Use `PATCH /verifications/{id}/application` with the complete replacement `fields` and complete
actual `address` to repair that same row. The API validates the current form and requires the
sender identity and address to match the retained application or authoritative provider data.
It refuses identity changes and unverifiable identity with actionable 409 guidance; the brand
and row remain intact. A provider lookup outage returns 503 without changing the application.
If a previous campaign create succeeded but its response was lost, the API recovers that
existing filing before allowing changes. The PATCH performs no paid provider action: it clears
the information hold only after validation, then a later poll resumes campaign filing and its
normal review fee. An identical retry is safe, including after that campaign has been filed.
Each accepted change writes the audit row `verification.amend` with the caller; it holds no answers.

CLI recovery uses `hail numbers get-sms-application <verification-id>` followed by
`hail numbers amend-sms <verification-id> --field ... --address ...`. Supply the complete current
answers and address; these are not partial field updates. MCP exposes `get_sms_application`
and `amend_sms_application`; amendment requires explicit `attest_authorized=true` after the
user reviews the corrections. Both preserve the existing ID and use authenticated API routes.
After an uncertain timeout, check that same registration before retrying. Neither recovery
client creates or cancels a registration as a fallback.

Before a first filing, MCP clients call `preview_sms_application` with the answers they will submit.
It returns the campaign text a carrier reviews. It files and charges nothing.

Each organization registers its own US A2P 10DLC brand and campaign. Fees are priced and
charged per step by the website's US texting rater. The console reviews the whole application
before submission. `POST /verifications/sms/preview` validates the answers and returns the
compiled campaign without a provider filing or charge. It takes `subject_type`, `number_id`,
`fields` and `address`; the number must belong to the caller organization. Toll-free verification is a separate provider process; this local-number
registration does not verify toll-free messaging.

| State             | Meaning                                                                           |
| ----------------- | --------------------------------------------------------------------------------- |
| `awaiting_review` | Profile and brand created at the carrier, not yet submitted.                      |
| `submitting`      | Hail is submitting the brand.                                                     |
| `submitted`       | Brand in vetting, or brand approved and campaign in carrier review.               |
| `approved`        | Campaign approved and required setup complete; each number must also be attached. |
| `rejected`        | Brand or campaign refused. Customer-facing guidance is sanitized.                 |
| `cancelled`       | Customer withdrew or dismissed.                                                   |

- **Fees.** This repo holds no prices and takes no money for a registration. Hail Cloud's website rater charges each step at its private prices, keyed by the `carrier_verifications` row: a submission (any row except one that reused a brand, `provider_refs.brand_reused`, and including a brand the carrier billed and then refused, saved as `rejected` with `provider_refs.refused_at_filing`), and a carrier review (`provider_refs.campaign_filed_at`, which the API writes once the plug-in's `campaign_ref` key holds a campaign; a data migration added it to rows that held a campaign before). The API pings it after each commit that changes a row's state or `provider_refs` (`notify_sms_registration_changed`, `POST /api/internal/sms-registrations/rate`); a retry that found the carrier down changes nothing and sends no ping. The rater is idempotent and also sweeps. Cancel and reject empty `provider_refs` (the private `application`, `brand_reused` and, on a cancel, `kept_brand_id` stay), so when the refs hold a campaign id the API first calls the rater and waits for its answer (`rate_sms_registration`, up to 5 seconds, before taking the organization's lock), then wipes them. Monthly fees are rated by the website too. The API only checks that the balance covers the carrier cost (`costs/sms-registration.json`) before filing (402). With a kept brand that cost is the campaign review alone. A kept brand that cannot be reused, with too little credit for a new brand, returns 402 and files no brand. Rows filed before this change carry a single `sms_registration:<id>` debit and are not charged again.
- **Campaign credit check.** Before a poll files the campaign, the API checks that the credit covers the campaign review (`campaign_vetting_usd` in `costs/sms-registration.json`). The credit is the balance less the steps of the last day the rater has not charged yet: a filing with no `sms_registration:<id>:submission` debit, a campaign with no `:review` debit. It goes to the organization's registrations before their campaign, oldest first; a poll the check lets file a campaign first writes `provider_refs.campaign_reserved_at` (database time) under the lock and clears it when the carrier answers. While it is set (at most 15 minutes, for a process that died in the call) that row keeps first claim, so another check during the carrier call cannot give the same credit to another row. A failed call keeps it, since a campaign it filed may come to light on the next poll. Another poll that finds a running reservation files nothing that time (it still asks the carrier, so a lost campaign is adopted), does not renew it, and drops it once the carrier answers. So a confirmed code on such a row files the campaign on a later poll: after that answer, or once the reservation runs out. Not covered: the plug-in's `status()` still runs (`may_file_campaign=False`): brand reads, rejections, code resends and adopting a campaign whose answer was lost all go on, and only the paid campaign call is skipped. The row shows `waiting_for_credit: true`; the next poll tries again. The poll reads the balance under the organization's lock but does not hold it through the carrier call; when the lock is busy, that poll files no campaign. The submit (sweeper retry, superadmin approve) is free and is not checked. The brand's own vetting (Telnyx, a company) is part of the submission, paid at filing, so it is not checked again. The shared key's organization skips the check.
- **Credit needed.** A waiting row also shows `credit_needed_cents`: its own share of the credit still needed. The first waiting row, in the order above, needs its fee less what is left after the covered rows (more when that is below zero, rounded up to a whole cent). Each later one needs its whole fee. So the sum over the waiting rows is one top-up for all; the console asks for that sum. It is in carrier cost (the review and the steps not yet charged), so it falls short if Hail's price of a step is ever above the carrier's.
- **Adding a carrier.** One plug-in module (a `SmsRegistrationProvider`) plus one `register_verification_provider(..., purpose="sms")` call that registers the class itself. The plug-in sets `campaign_ref` (the refs key of its filed campaign; a test fails without it), `field_limits` (its maximums for fields of the shared form in [`sms_form.py`](../../core/hailhq/core/providers/verification/sms_form.py)) and `extra_fields` (fields only it asks for, such as `text_types_field`). The shared form never checks a carrier's name. Hail Cloud also needs a price block for the carrier in the website; until then it charges the default prices.
- **Attach step.** After the campaign is approved, Hail attaches the organization's US long-code numbers to it, each on its own, and records each one in `phone_numbers.sms_registration_id`. The send gate lets a US-to-US text leave a long code only when that column points at an approved registration; `POST /sms` without `from` prefers such a number. A person's registration holds one number: on Twilio it sits in its own messaging service (`hail-org-<org>-person`) that holds only that number. It takes the oldest number on no registration. Any number recorded on the registration (`sms_registration_id`) counts as its number, even one no longer eligible: a released number still recorded on it keeps the registration, and no other number joins until support clears that number's `sms_registration_id`. While the carrier refuses that number for this registration, the verification worker tries the next one on its next pass, and so on; the first number that attaches is the person's number and no other joins. On Telnyx, the plug-in first lists the campaign's numbers (`GET /10dlc/phone_number_campaigns`, every page). Only `FAILED_ASSIGNMENT` and `PENDING_UNASSIGNMENT` leave the campaign free; any other `assignmentStatus`, a missing or unknown one too, holds it. The same number counts as attached only when its status is `ASSIGNED` (a link that timed out may have gone through); in any other holding status, a missing or unknown one too, the link is sent again, and a 409 on it counts as attached. Another number holding it is recorded as attached at once only when its status is `ASSIGNED`, no number is on the registration in Hail yet, and it is one of the organization's eligible numbers on that carrier and on no registration (checked again with its row locked). The number tried is then not refused, and its wait for this registration is cleared. In every other case the number tried is refused with a reason that names the held number and its status, for example `+15125550100 (PENDING_ASSIGNMENT)`; the worker tries again after the wait. Two or more numbers holding it: the reason names them all and Hail records none; unlink the extra numbers at Telnyx so one is left. A held number that is not one of the organization's: unlink it at Telnyx. When `POST /numbers/{id}/enable-sms` records a held number, its `verification.sms_attached` row holds `adopted` (the number recorded) and `tried` (the number in the request): read it to see why the number in the request did not attach. A failed list call, or a page that repeats only records already read, sends no link; the refusal row holds the reason (for a failed call, Telnyx's status and error text). The row is approved even when a number fails; the failure is logged with the number id and counted in the `verification.sms_attached` audit row (`failed`). The first refusal of a number not yet on the registration, on any path, writes a `verification.sms_attach_refused` audit row: `resource_id` is the number, and the payload holds `e164`, `verification_id` and `carrier_reason`. Support reads it to see why that number's texts return 422. A later refusal writes a new row only when the carrier's reason differs from the last one written, so a refusal after a carrier outage does not hide behind the outage text. Read the newest row for the number. The verification worker then waits 1 hour, doubling up to 24 hours, before it tries that number again (`phone_numbers.provisioning_metadata['sms_attach_refused']` holds the registration, `wait_hours`, `retry_at` and `reason_hash`, the first 16 hex digits of the SHA-256 of the last reason written; each refusal doubles the wait, a success clears it, and another registration replaces it). A repair pass writes a `verification.sms_attached` row only when a number attached, and `failed` counts only the numbers it tried. `POST /numbers/{id}/enable-sms` ignores the wait and tries that number at once. For a person, it tries the number when it is the one already on the registration (a re-attach), or when no number is on the registration and every older free number was refused for it. It answers 200 even if the carrier refuses, so check `phone_numbers.sms_registration_id` and the audit row. Until approval, texts from a US long code to a US number return 422.
- **Telnyx brand wait.** Telnyx registers a new 10DLC brand in the background: for about 2 seconds the brand id gets 404 from `smsOtp` and `externalVetting`, and 400 from `DELETE`. The draft reads the brand (up to 10 reads, 1 second apart) before it sends the PIN or asks for vetting. A refused brand (`failureReasons`), or a 400 or 422 on the PIN or vetting call that names an application field (`source.pointer`, plain or under `/body`) or a known field word and no account or configuration problem (`application_validation_error` in [`sms_review.py`](../../core/hailhq/core/providers/verification/sms_review.py)), is a 422 on the form fields it names; Telnyx bills the brand anyway, so it is kept as a `rejected` row (`provider_refs.refused_at_filing`) and the website charges its submission. A brand still registering after the wait, or one that exists when any later step fails another way (5xx, 429, 404, no answer, a body that is not JSON or not the expected shape), is kept as a normal filing (a retry would bill a new brand), and the background pass sends the PIN (`otp_sent_at` unset) or asks for vetting (`refs.vetting_requested` is `false`; a row without the key was filed before it existed and already asked). Each vetting request is billed, and one that timed out may have reached Telnyx: before asking again the background pass lists the brand's vettings (`GET /10dlc/brand/{id}/externalVetting`); if one is listed, or Telnyx answers the request with error `10012` (a vetting in progress or passed), it only sets `vetting_requested` to `true`. A read that fails with 429 or 5xx, or whose body is not JSON, counts as "still registering". The background pass does nothing while the brand is `REGISTRATION_PENDING`, and rejects a `REGISTRATION_FAILED` brand.
- **Campaign classification.** Both forms collect one to five actual messaging purposes. Company campaigns use a single purpose when selected, or `MIXED` for multiple purposes. Telnyx sends sub-use cases only for mixed and sole proprietor filings. Samples cover every purpose: when each sample names a purpose, every chosen purpose has one; when some samples name none, those samples cover the rest, one each. One sample needs STOP. The program name must be in each sample, ignoring case, spaces and dots. Link, phone-number and content attributes come from validated answers. Legacy brand-only snapshots missing the new evidence need correction before a new campaign filing; already-filed campaigns continue normal status recovery.
- **Readiness.** The verification step can be `confirm_identity`, `filing`, `carrier_review`, `needs_information` or `runtime_setup`. `needs_information` is a hold for evidence repair on the existing paid sender, not a rejection. `campaign_accepted` records the carrier decision; `runtime_ready` records verified runtime setup, or is null where not independently tracked. Number attachment remains separate and the send gate checks each number's registration link.
- **Telnyx STOP and HELP replies.** Hail configures the filed responses on the messaging profile. If setup fails, the same accepted campaign remains pending in `runtime_setup` and later polls retry setup. There is no approval fallback after a retry count. Telnyx requires the actual subscription confirmation before creating a paid brand. Keyword enrollment also requires it on Twilio. The submitted confirmation must include the program name, contain the exact declared frequency, message/data-rate disclosure, HELP and STOP. Common wording such as “Msg & data rates” is accepted. Invalid content returns field-specific errors before paid filing; Hail never invents missing text. This helper configures STOP/HELP only, not an initial subscription reply: the applicant must already operate the confirmation they declare. Keep `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false` when the provider sends replies, to avoid duplicates. Test actual account behavior before making claims about it.
- **Person confirm step.** A person's brand waits on the person. The row shows `confirmation`: `code` (Telnyx texts a PIN; the person enters it in Hail), `reply` (Twilio texts them; they answer that text), or `null`. A code goes to `POST /verifications/{id}/confirm`; on success Hail marks `identity_verified` in the refs and files the campaign at once. It waits when the credit does not cover it, while another poll's reservation holds the row (its call is running or failed), or while another transaction holds the row lock (see **Campaign credit check**). A wrong or expired code is 422 on `code`; a carrier that does not answer is 503; anything else is 409. After 5 wrong codes the route returns 429 without calling the carrier, until a new code is sent (`refs.confirm_attempts` resets to 0). Telnyx sends a new PIN when the last one is over 24 hours old (on the next poll). The code is never stored or logged; the `verification.confirm` audit row holds `{"ok", "attempts"}`, never the code.

  ```bash
  curl -s -X POST "$HAIL_API_URL/verifications/$VERIFICATION_ID/confirm" \
    -H "Authorization: Bearer $HAIL_API_KEY" -H "Content-Type: application/json" \
    -d '{"code":"482913"}'
  ```

  CLI: `hail numbers confirm-sms <verification-id> --code 482913`.

- **`TWILIO_PRIMARY_PROFILE_SID`.** The approved Trust Hub primary profile (business identity ISV Reseller or Partner) that every Twilio brand is created under. It is required for local 10DLC registration. Toll-free verification and read-only approval checks need the Twilio credentials but do not use this local setting.

- **Owner emails.** When a verification becomes `approved` or `rejected` (carrier poll, superadmin reject, a draft the carrier refuses, a bought number whose registration is refused), the API pings the website after the commit (`notify_verification_decided`, `POST /api/internal/verifications/decided`). Session listeners in [`verification_decisions.py`](../../api/hailhq/api/verification_decisions.py) send the ping for any row whose `state` changed to one of those, so a new path needs no extra call. Polls lock the row, so two polls at once send one email. Hail Cloud's website emails the org's owners. A ping is fire-and-forget: if the website is down, that email is not sent. Self-hosted installs without the website send nothing.

## Cancel rules

`DELETE /verifications/{id}`:

- `awaiting_review` → `cancelled`, draft discarded at the carrier.
- `rejected` → `cancelled` (dismiss), draft discarded. Exception for a Telnyx person (`purpose=sms`) whose PIN was confirmed and whose campaign was filed: only the campaign is deleted, the brand stays at Telnyx, and the cancelled row holds `provider_refs.kept_brand_id`. The next filing reuses that brand when Telnyx still shows it confirmed and first name, last name, email and mobile match: no new brand, no PIN, `provider_refs.brand_reused`, no submission fee. A kept brand Telnyx answers 404 or 410 for is gone; any other error is a retryable 503, so an auth or account error never buys a second brand. Otherwise it files a new brand, or returns 402 with no brand when the credit does not cover one. The kept brand then stays at Telnyx, as does the brand of a customer who never files again: Telnyx refuses to delete a brand for 3 months after its campaign.
- `submitting`, `submitted` → **409** while review is pending. Exception: an SMS campaign already accepted by the carrier but waiting in `runtime_setup` (`campaign_accepted=true`, `runtime_ready=false`) can be cancelled. The API rates the existing campaign before discarding it and rechecks eligibility under locks, preserving normal billing and provider cleanup behavior. A `needs_information` registration is repaired with PATCH on its existing row, not cancellation. Every verification response carries `cancellable`: true when `DELETE` accepts the row now. Clients show Cancel from it.
- `approved` with `purpose=sms` → `cancelled`. Each number on it (`phone_numbers.sms_registration_id`) leaves the campaign (Telnyx unlink; a person's Twilio number leaves the person's service and is set up in the org's service again), then the campaign, brand, bundle and profile are discarded. `cancelled_at` is set; the website rater bills the monthly fee through that month. Telnyx bills at least 3 monthly fees from approval, so for Telnyx the rater bills through the later of the cancel month and the approval month + 2 (`minimum_months` in `costs/sms-registration.json`). Audit row `verification.cancel`.
- `approved` with `purpose=number`, `cancelled` → 409.

## Superadmin routes

`/admin/verifications` (list, `POST /{id}/approve`, `POST /{id}/reject`) are not in the OpenAPI spec. Only a console session the website minted with `superadmin: true` passes ([`api/hailhq/api/superadmin.py`](../../api/hailhq/api/superadmin.py)); API keys never do. The console page lives in hail-website at `app/console/admin/verifications/`. Approve re-checks the draft with the carrier, then submits; reject records the reason and discards the draft. If the row changed while a filed campaign was rated (the background pass submitted it, say), reject answers 409 and discards nothing. If the discard fails, reject answers 502 and the row keeps its state and ids. Both write an audit row (`verification.approve`, `verification.reject`) with the admin as actor.

## Where to look

- Routes and the background pass: [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py).
- Worker loop: [`api/hailhq/api/verification_worker.py`](../../api/hailhq/api/verification_worker.py).
- Carrier plug-in contract (`requirements`, `create_draft`, `check`, `submit`, `status`, `purchase_handle`, `discard`, `subject_name`, `fetch_subject_name`): [`core/hailhq/core/providers/verification/base.py`](../../core/hailhq/core/providers/verification/base.py). Twilio: [`twilio.py`](../../core/hailhq/core/providers/verification/twilio.py) next to it.
- Tests: [`api/tests/test_verifications_api.py`](../../api/tests/test_verifications_api.py) (`FakeCarrier` stands in for the carrier). Run: `cd api && uv run pytest tests/test_verifications_api.py -q`.
- Is the pass running? `docker compose logs api | grep "verification worker"`. The line appears only on a pass that changed something.
- Audit trail: `SELECT action, actor_kind, payload FROM audit_log WHERE target_type = 'carrier_verification' ORDER BY created_at DESC LIMIT 20;`

### SMS recovery regression coverage

- [Shared form tests](../../core/tests/test_sms_form.py) cover missing confirmation sender/frequency/rates/HELP/STOP, accepted rate abbreviations, and whitespace-preserving content attributes and consent claims.
- [Telnyx registration tests](../../core/tests/test_telnyx_sms_registration.py) and [Twilio registration tests](../../core/tests/test_twilio_sms_registration.py) cover legacy brand-only evidence repair, lost campaign-create response recovery and provider-outage retries without a second paid filing. Existing campaign approval tests (`test_status_approved_and_rejected_from_campaign`, `test_status_approved_when_campaign_verified`) exercise already-filed applications independently of the new-form gate.
- [Verification API tests](../../api/tests/test_verifications_api.py) cover same-row amendments, original-identity enforcement, organization isolation, idempotent retries and cancellation during runtime setup. Existing `test_cancel_an_approved_sms_registration` and approved-registration attachment tests retain completed-registration coverage.
- [CLI recovery tests](../../cli/internal/cmd/number_sms_application_test.go) and [MCP verification tests](../../mcp/tests/test_verification_tools.py) assert authenticated same-ID GET/PATCH requests, no create/delete fallback, payload bounds, sanitized field errors and readiness in JSON.

## Known gaps

- `VerificationResponse.provider` returns the carrier name to the customer. It predates the "never name a carrier" rule and has not been changed.

## Toll-free texting verification

Use `purpose=sms`, `number_type=toll_free`, the number's `country_code` (`US` or
`CA`), and its owned `number_id`. Read `/v1/verifications/requirements` first.
Each verification covers exactly one number. `carrier_verifications.number_id`
links that number and permits separate live requests for separate toll-free
numbers. This path currently supports Twilio only.

Toll-free verification is free. It creates no 10DLC brand, campaign, OTP or fee.
The API does not notify the 10DLC fee rater. Before a Cloud filing, it checks the
website's signed `POST /api/internal/sms-registrations/capabilities` response for
`{"toll_free":{"supported":true,"filing_cents":0,"monthly_cents":0}}`.
An old, unavailable or malformed website response stops filing before any carrier
write or verification row. Self-hosted installs with no website need no handshake.
Deploy Hail first, then the website's free-TFV billing guards and UI. New filings
stay unavailable during that gap. Keep those guards when rolling back a website
that already has toll-free verification rows.

Submit the end business's real identity, contact, address and public business
website or social page. Supply clear message purposes, branded samples, exact
opt-in steps, separate optional consent and public redacted evidence for every
selected method. Verbal consent needs a script document. Marketing needs written
consent. Dedicated privacy and terms URLs are always filed. Hail never fetches
these arbitrary evidence URLs. Public email domains are not rejected locally.

A lost create response is recovered by the owned carrier number and a hash of
the exact submitted answers. Existing unmatched carrier requests return a 422 on `number_id` that names support.
Editable rejections keep the same row in `submitted` / `needs_information`.
`PATCH /v1/verifications/{id}/application` checks the original sender, confirms
that the carrier still permits edits, and updates the same carrier request.
The send gate blocks US and Canadian recipients until this exact number has an
approved verification. Another number's approval never unlocks it. Existing
manual Twilio approvals continue through a read-only check: the current incoming
phone resource must match the owned SID, E.164 number and authenticated account.
Every TFV record read must match the same SID/account, and the newest one must be
`TWILIO_APPROVED`. An error, timeout or mismatch fails closed. A "yes" is kept
for 10 minutes in each API process, so a revoked approval stops sends within
10 minutes. A "no" is not kept. This check writes no provider or
Hail registration and stores no guessed customer identity. Other carriers keep
their existing carrier-side checks until Hail supports their TFV API.

Official guidance checked November 14, 2026:
[approval guidance](https://www.twilio.com/en-us/blog/insights/best-practices/improving-your-chances-of-toll-free-verification-approval),
[API onboarding](https://www.twilio.com/docs/messaging/compliance/toll-free/api-onboarding),
[verification API](https://www.twilio.com/docs/messaging/api/tollfree-verification-resource).
The SDK regression tests use the pinned real SDK with an offline HTTP recorder.
No live provider filing, message, charge or refund is part of local validation.
