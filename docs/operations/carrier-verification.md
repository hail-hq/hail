# Carrier verification runbook

> **For developers and coding agents:** this page is self-contained. It says what carrier verification is, how a verification moves from created to approved, what runs in the background, what to configure, and where the code and tests are.

## What this is

Some countries require the buyer of a phone number to be verified first (ID, address, or a regulatory bundle). Hail collects the details in the console, files them with the carrier, and follows the carrier's decision. Once approved, `POST /numbers` can buy numbers of that country and type for the organization.

- Twilio is the only carrier plug-in in v1. It is registered in [`core/hailhq/core/providers/verification/__init__.py`](../../core/hailhq/core/providers/verification/__init__.py) and is active whenever `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` are set.
- Hail stores state, opaque carrier IDs, and the name of the person or company (`subject_name`). Number verification keeps the customer's answers and an encrypted copy of each file for 30 days after submit, for a resubmit (see [Kept answers and files](#kept-answers-and-files-resubmit)). SMS registration retains submitted text fields and address privately for application repair, until 30 days after a cancel or rejection, or until the account closes, whichever comes first; document files are never kept on the verification (table `carrier_verifications`, [`core/hailhq/core/models.py`](../../core/hailhq/core/models.py)). A document sent with an upload link (`POST /v1/uploads`, then the `upload.<slot>` part) is stored until the verification is saved, then it is deleted. The GC (`core/hailhq/core/email_attachment_gc.py`, every 10 minutes) deletes any left 1 hour after upload. `POST /v1/verifications/check` runs every check before filing and saves nothing. The name is never logged.
- **At account close** (`POST /internal/org-closures`, then every background pass for each org in `org_closures`), [`close_org_carrier_items`](../../api/hailhq/api/org_close.py) first releases the org's numbers and cancels its registrations at the carrier (see [Cancel rules](#cancel-rules)). Then [`clear_closed_org_registrations`](../../core/hailhq/core/retention.py) deletes the saved answers and the text made from them (`provider_refs` keys in `REGISTRATION_ANSWER_REFS`: `application`, `application_amendment_hash`, `campaign`, `replies`, `subject_name`), `rejection_reason`, and `subject_name` (except on a live number verification, which a unique index needs named). It keeps the state, dates, carrier ids and billing keys. It logs counts only. hail-website's account deletion calls `POST /internal/org-closures`; an account deleted before that has no closure row, so nothing is cleared for it.
- **UK (GB):** a new number verification for Telnyx GB local, mobile or toll-free cannot start (Hail sells those from Twilio only), and Telnyx GB national takes a company only (a person gets 422, `ctx.subject_types: ["business"]`). Existing rows still load, poll and cancel. Rules and reason: [number-catalog-sync.md](number-catalog-sync.md#rules-the-sync-cannot-hold).
- **Customer-facing text never names a carrier.** Error details, docstrings (they become OpenAPI descriptions), and console copy say "the carrier", never "Twilio" or "bundle".

## Configuration

| Variable                                  | Default                                                                     | Where                                                                    | Effect                                                                                                                               |
| ----------------------------------------- | --------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ |
| `HAIL_VERIFICATION_POLL_SECONDS`          | `600` (in [`core/hailhq/core/config.py`](../../core/hailhq/core/config.py)) | `.env` on the host running `api` (`/opt/hail/.env` on the production VM) | Seconds between background passes. `0` disables the pass.                                                                            |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | empty                                                                       | same `.env`                                                              | Without both, no verification provider is configured and `POST /verifications` answers 404 `no verification provider is configured`. |

Nothing else needs to be set. A `.env` without `HAIL_VERIFICATION_POLL_SECONDS` runs the pass every 600 seconds because the code default applies. The pass starts inside the API process at boot ([`api/hailhq/api/main.py`](../../api/hailhq/api/main.py), `VerificationWorker`); there is no separate container.

## Lifecycle

| State             | Meaning                                                                                                                     | Who moves it here                                                                           |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `awaiting_review` | Draft exists at the carrier, not yet sent for review.                                                                       | `POST /verifications`, or a submit that failed because the carrier was down.                |
| `submitting`      | Hail is sending it to the carrier right now.                                                                                | `_submit_row` before the carrier call (so a crash cannot submit twice).                     |
| `submitted`       | Carrier is reviewing.                                                                                                       | `_submit_row` after the carrier accepted.                                                   |
| `approved`        | Carrier approved. Numbers can be bought.                                                                                    | `GET` refresh or the background pass, from the carrier's status.                            |
| `rejected`        | Carrier or a superadmin refused. `rejection_reason` says why and what to fix (see [Rejection reasons](#rejection-reasons)). | Carrier status, superadmin reject, or the background pass when the carrier refuses a draft. |
| `cancelled`       | Customer withdrew or dismissed it.                                                                                          | `DELETE /verifications/{id}`.                                                               |

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
3. When the evaluation passes, the row is saved as `awaiting_review` and submitted to the carrier in the same request. There is no human gate. If the carrier is unreachable the row stays `awaiting_review`; the background pass retries. If the carrier refuses to take the draft, the row is `rejected` and the draft discarded (superadmin approve answers 409). If the discard fails on an outage, the row keeps its ids and goes back to `awaiting_review` (approve still answers 409); if the carrier refuses the delete, the row is `rejected` with `discard_refused` (see [Cancel rules](#cancel-rules)).
4. `GET /verifications/{id}` and `GET /verifications` ask the carrier for the current status at most once a minute per row and update `submitted` rows to `approved` or `rejected`.
5. Every `HAIL_VERIFICATION_POLL_SECONDS` the background pass (`sweep_verifications` in [`api/hailhq/api/routes/verifications.py`](../../api/hailhq/api/routes/verifications.py)) does:
   - `awaiting_review` rows untouched for 2 minutes: lock the row (`FOR UPDATE SKIP LOCKED`), ask the carrier to check the draft. Pass → submit. Refused → `rejected` with the carrier's message, draft discarded, audit `verification.reject` by `system`. Before the discard, ids the carrier call wrote are committed, and the row is locked again (waiting, not skipping); a row that changed meanwhile is left as it is. Discard fails on an outage → the row keeps its ids and its state; the carrier refuses the delete → `rejected` with `discard_refused`. Carrier down → the ids it wrote are saved; try next pass. A check, submit, status poll, or discard (of a refused draft or a superadmin reject) that fails partway keeps only the ids it added (new keys, empty values filled in, items appended to a list, the same inside nested objects); keys it removed or changed go back to what they were. A customer cancel's discard is not covered: it fails as a whole.
   - `submitting` and `submitted` rows: refresh from the carrier. A `submitting` row older than 5 minutes is settled from the carrier's status. A poll that fails keeps only the ids it added, as above.
   - Last, in its own step: asks the carrier whether each US trial texting number is still approved ([sms-trial-numbers.md](sms-trial-numbers.md)).
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
An MCP agent cannot send that multipart request, so it makes an upload link with purpose
`sms_evidence` (`POST /v1/uploads`, MCP `create_file_upload`) and sends the bytes with `curl`.
That PUT follows the same rules (type, size, cap, public URL) and returns the same `url`. The
PUT deletes the link's `file_uploads` row as it uses it, so no row tracks the file.
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

The consent disclosure check (`_disclosure_gaps` in `sms_form.py`) accepts each policy by its
web address or by its link words, because a real checkbox links the words. Accepted:
"Privacy Policy", "Privacy Notice", "SMS Terms", "Terms of Service", "Terms and Conditions",
and a bare "Terms" next to the privacy policy ("Privacy Policy and Terms", "Terms and Privacy
Policy"). Not accepted: "your privacy", "in terms of", "Terms apply". The web addresses always reach the carrier in their own campaign
fields (`_policy_links` in `twilio_sms.py` and `telnyx_sms.py`). A failed check names only the
missing parts.

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

- **Fees.** This repo holds no prices and takes no money for a registration. Hail Cloud's website rater charges each step at its private prices, keyed by the `carrier_verifications` row: a submission (any row except one that reused a brand, `provider_refs.brand_reused`, and including a brand the carrier billed and then refused, saved as `rejected` with `provider_refs.refused_at_filing`), and a carrier review (`provider_refs.campaign_filed_at`, which the API writes once the plug-in's `campaign_ref` key holds a campaign; a data migration added it to rows that held a campaign before). The API pings it after each commit that changes a row's state or `provider_refs` (`notify_sms_registration_changed`, `POST /api/internal/sms-registrations/rate`); a retry that found the carrier down changes nothing and sends no ping. The rater is idempotent and also sweeps. Cancel and reject empty `provider_refs` (the private `application`, `brand_reused`, `low_volume`, `faster_lane_at` and, on a cancel, `kept_brand_id` stay), so when the refs hold a campaign id the API first calls the rater and waits for its answer (`rate_sms_registration`, up to 5 seconds, before taking the organization's lock), then wipes them. Monthly fees are rated by the website too. The API only checks that the balance covers the carrier cost (`costs/sms-registration.json`) before filing (402). With a kept brand that cost is the campaign review alone. A kept brand that cannot be reused, with too little credit for a new brand, returns 402 and files no brand. Rows filed before this change carry a single `sms_registration:<id>` debit and are not charged again.
- **Low volume (companies only).** The company form asks `daily_texts` ("How many texts do you expect to send each day?", optional): `NOT_SURE` "Not sure yet", `UP_TO_2000` "Up to 2,000", `OVER_2000` "More than 2,000". Blank, `NOT_SURE` and `UP_TO_2000` file low volume; `OVER_2000` files the vetted brand. A blank answer files at low volume: so does every client that omits the field. A carrier with no `business_low_volume` row in `costs/sms-registration.json` has no low-volume filing: the API turns the answer into `OVER_2000` (`_carrier_daily_texts`, at filing and on an amendment), so the company files the vetted brand at the `business` prices and the row gets no `low_volume` mark. The API makes the choice once (`sms_form.low_volume`): it prices the filing, goes to the plug-in as `create_draft(low_volume=True)`, and is written as `provider_refs.low_volume = true` (also on Telnyx's billed refs). Twilio files a Low-Volume Standard brand (`skip_automatic_sec_vet=true`, still `STANDARD`). Telnyx sends no `externalVetting` at filing and never sets `vetting_requested`. The campaign is `LOW_VOLUME`. Limits the customer accepts: about 2,000 segments a day to T-Mobile, the lowest throughput, no trust score. An amendment that omits `daily_texts` keeps the filed answer; a different answer is a 422 on `daily_texts` (moving to a vetted brand is a new registration). `_wiped_refs` keeps `low_volume` and `faster_lane_at`.
- **Faster lane (low volume only).** Optional box `faster_lane_ok` ("Move me to the faster lane automatically if the networks need more details about my company.", value `YES`, read like any choices value), stored as `provider_refs.faster_lane_ok = true`. Only a carrier with `faster_lane` (Telnyx) shows it; the API ignores it for Twilio. It matters once the carrier has not verified the company: `identityStatus` `UNVERIFIED` at once, or any other value that is not verified for 24 hours after the first pass saw it (`provider_refs.identity_wait_since`; Telnyx does not document which values are final). Never pending forever:
  - Telnyx, ticked: when the campaign credit check passes (it counts the faster lane fee on top of the campaign review while the lane is owed), the pass writes `provider_refs.faster_lane_at` (ISO time; the website charges the vetting from it, ledger ref `sms_registration:<id>:faster_lane`) and `vetting_requested = false`, then asks for STANDARD Aegis vetting once (`_request_vetting_once`). The vetted flow then files the `LOW_VOLUME` campaign. A failed request keeps both keys, so the next pass asks again. Without the credit, nothing is bought and the row shows `waiting_for_credit`. Once the vetting ends and the brand is still not verified, the row goes to `needs_information` with a reason that asks for a new registration (`LOW_VOLUME_FASTER_LANE_FAILED`): a vetting record listed as `FAILED` or `EXPIRED` holds at once; `ACTIVE` holds 24 hours after its `vettedDate` (or `faster_lane_at`), so a brand read that lags is not held. Telnyx lists only `ACTIVE` vettings unless `vettingStatus` names others, so the pass asks for all three. The hold also sets `provider_refs.faster_lane_failed`, the one hold the background pass keeps polling: each pass only reads the brand (no new request, no fee). If Telnyx verifies the company later, the pass drops `needs_information`, `faster_lane_failed` and the reason, and the next pass files the campaign once the credit check passes. The customer can still cancel while it is held.
  - Telnyx, verified: once a low-volume brand reads `VERIFIED` or `VETTED_VERIFIED`, the pass writes `provider_refs.brand_verified = true`. The lane is then no longer owed: no pass buys it, and neither credit check nor an amendment counts its fee.
  - Telnyx, not ticked: the row goes to `needs_information` with a reason that offers the faster lane. An amendment that ticks the box (402 when the credit does not cover `faster_lane_usd` plus the uncharged steps) lets a later pass ask for the vetting.
  - Twilio: the row goes to `needs_information` with a reason that asks for a new registration. Twilio's API cannot add vetting to a Low-Volume Standard brand: its [Brand Vetting resource](https://www.twilio.com/docs/messaging/api/brand-vetting-resource) files Campaign Verify tokens only.
  - A `submitted` row held in `needs_information` with no campaign filed (`campaign_filed_at` unset) can be cancelled; the carrier cleanup runs as for any cancel. Toll-free rows never set `campaign_filed_at`, so this also covers a toll-free row held after a rejection the customer may edit.
- **Low-volume fees.** The `business_low_volume` row in `costs/sms-registration.json`: brand $4.50, campaign review $15, $1.50 a month, `faster_lane_usd` $41.50 (Telnyx only), marked `verification_method: unverified`. The 402 floor adds `faster_lane_usd` when the box is ticked (the worst case). So does the campaign credit check, while the lane is owed: box ticked, no `faster_lane_at`, and no `brand_verified`. Uncharged filings of the last day count whole at their own row's price, an owed faster lane included (`_uncharged_cents(filing_default=...)`), and a bought faster lane not yet charged counts too.
- **Campaign credit check.** Before a poll files the campaign, the API checks that the credit covers the campaign review (`campaign_vetting_usd` in `costs/sms-registration.json`). The credit is the balance less the steps of the last day the rater has not charged yet: a filing with no `sms_registration:<id>:submission` debit, a campaign with no `:review` debit. It goes to the organization's registrations before their campaign, oldest first; a poll the check lets file a campaign first writes `provider_refs.campaign_reserved_at` (database time) under the lock and clears it when the carrier answers. While it is set (at most 15 minutes, for a process that died in the call) that row keeps first claim, so another check during the carrier call cannot give the same credit to another row. A failed call keeps it, since a campaign it filed may come to light on the next poll. Another poll that finds a running reservation files nothing that time (it still asks the carrier, so a lost campaign is adopted), does not renew it, and drops it once the carrier answers. So a confirmed code on such a row files the campaign on a later poll: after that answer, or once the reservation runs out. Not covered: the plug-in's `status()` still runs (`may_file_campaign=False`): brand reads, rejections, code resends and adopting a campaign whose answer was lost all go on, and only the paid campaign call is skipped. The row shows `waiting_for_credit: true`; the next poll tries again. The poll reads the balance under the organization's lock but does not hold it through the carrier call; when the lock is busy, that poll files no campaign. The submit (sweeper retry, superadmin approve) is free and is not checked. The brand's own vetting (Telnyx, a vetted company) is part of the submission, paid at filing, so it is not checked again. The shared key's organization skips the check.
- **Credit needed.** A waiting row also shows `credit_needed_cents`: its own share of the credit still needed. The first waiting row, in the order above, needs its fee less what is left after the covered rows (more when that is below zero, rounded up to a whole cent). Each later one needs its whole fee. So the sum over the waiting rows is one top-up for all; the console asks for that sum. It is in carrier cost (the review and the steps not yet charged), so it falls short if Hail's price of a step is ever above the carrier's.
- **Adding a carrier.** One plug-in module (a `SmsRegistrationProvider`) plus one `register_verification_provider(..., purpose="sms")` call that registers the class itself. The plug-in sets `campaign_ref` (the refs key of its filed campaign; a test fails without it), `field_limits` (its maximums for fields of the shared form in [`sms_form.py`](../../core/hailhq/core/providers/verification/sms_form.py)) and `extra_fields` (fields only it asks for, such as `text_types_field`). The shared form never checks a carrier's name. Hail Cloud also needs a price block for the carrier in the website; until then it charges the default prices.
- **Attach step.** After the campaign is approved, Hail attaches the organization's US long-code numbers to it, each on its own, and records each one in `phone_numbers.sms_registration_id`. The send gate lets a US-to-US text leave a long code only when that column points at an approved registration; `POST /sms` without `from` prefers such a number. A person's registration holds one number: on Twilio it sits in its own messaging service (`hail-org-<org>-person`) that holds only that number. It takes the oldest number on no registration. Any number recorded on the registration (`sms_registration_id`) counts as its number, even one no longer eligible: a released number still recorded on it keeps the registration, and no other number joins until support clears that number's `sms_registration_id`. While the carrier refuses that number for this registration, the verification worker tries the next one on its next pass, and so on; the first number that attaches is the person's number and no other joins. On Telnyx, the plug-in first lists the campaign's numbers (`GET /10dlc/phone_number_campaigns`, every page). Only `FAILED_ASSIGNMENT` and `PENDING_UNASSIGNMENT` leave the campaign free; any other `assignmentStatus`, a missing or unknown one too, holds it. The same number counts as attached only when its status is `ASSIGNED` (a link that timed out may have gone through); in any other holding status, a missing or unknown one too, the link is sent again, and a 409 on it counts as attached. Another number holding it is recorded as attached at once only when its status is `ASSIGNED`, no number is on the registration in Hail yet, and it is one of the organization's eligible numbers on that carrier and on no registration (checked again with its row locked). The number tried is then not refused, and its wait for this registration is cleared. In every other case the number tried is refused with a reason that names the held number and its status, for example `+15125550100 (PENDING_ASSIGNMENT)`; the worker tries again after the wait. Two or more numbers holding it: the reason names them all and Hail records none; unlink the extra numbers at Telnyx so one is left. A held number that is not one of the organization's: unlink it at Telnyx. When `POST /numbers/{id}/enable-sms` records a held number, its `verification.sms_attached` row holds `adopted` (the number recorded) and `tried` (the number in the request): read it to see why the number in the request did not attach. A failed list call, or a page that repeats only records already read, sends no link; the refusal row holds the reason (for a failed call, Telnyx's status and error text). The row is approved even when a number fails; the failure is logged with the number id and counted in the `verification.sms_attached` audit row (`failed`). The first refusal of a number not yet on the registration, on any path, writes a `verification.sms_attach_refused` audit row: `resource_id` is the number, and the payload holds `e164`, `verification_id` and `carrier_reason`. Support reads it to see why that number's texts return 422. A later refusal writes a new row only when the carrier's reason differs from the last one written, so a refusal after a carrier outage does not hide behind the outage text. Read the newest row for the number. The verification worker then waits 1 hour, doubling up to 24 hours, before it tries that number again (`phone_numbers.provisioning_metadata['sms_attach_refused']` holds the registration, `wait_hours`, `retry_at` and `reason_hash`, the first 16 hex digits of the SHA-256 of the last reason written; each refusal doubles the wait, a success clears it, and another registration replaces it). A repair pass writes a `verification.sms_attached` row only when a number attached, and `failed` counts only the numbers it tried. `POST /numbers/{id}/enable-sms` ignores the wait and tries that number at once. For a person, it tries the number when it is the one already on the registration (a re-attach), or when no number is on the registration and every older free number was refused for it. It answers 200 even if the carrier refuses, so check `phone_numbers.sms_registration_id` and the audit row. Until approval, texts from a US long code to a US number return 422.
- **Telnyx brand wait.** Telnyx registers a new 10DLC brand in the background: for about 2 seconds the brand id gets 404 from `smsOtp` and `externalVetting`, and 400 from `DELETE`. The draft reads the brand (up to 10 reads, 1 second apart) before it sends the PIN or asks for vetting. A refused brand (`failureReasons`), or a 400 or 422 on the PIN or vetting call that names an application field (`source.pointer`, plain or under `/body`) or a known field word and no account or configuration problem (`application_validation_error` in [`sms_review.py`](../../core/hailhq/core/providers/verification/sms_review.py)), is a 422 on the form fields it names; Telnyx bills the brand anyway, so it is kept as a `rejected` row (`provider_refs.refused_at_filing`) and the website charges its submission. A brand still registering after the wait, or one that exists when any later step fails another way (5xx, 429, 404, no answer, a body that is not JSON or not the expected shape), is kept as a normal filing (a retry would bill a new brand), and the background pass sends the PIN (`otp_sent_at` unset) or asks for vetting (`refs.vetting_requested` is `false`; a row without the key was filed before it existed and already asked). Each vetting request is billed, and one that timed out may have reached Telnyx: before asking again the background pass lists the brand's vettings (`GET /10dlc/brand/{id}/externalVetting`); if one is listed, or Telnyx answers the request with error `10012` (a vetting in progress or passed), it only sets `vetting_requested` to `true`. A read that fails with 429 or 5xx, or whose body is not JSON, counts as "still registering". The background pass does nothing while the brand is `REGISTRATION_PENDING`, and rejects a `REGISTRATION_FAILED` brand.
- **Campaign classification.** Both forms collect one to five actual messaging purposes. Company campaigns use a single purpose when selected, or `MIXED` for multiple purposes. A low-volume company (above) files `LOW_VOLUME` whatever it picked. Telnyx sends sub-use cases only for `MIXED`, `SOLE_PROPRIETOR` and `LOW_VOLUME` filings; Twilio's campaign API has no sub-use-case field, so Twilio files the use case alone. Samples cover every purpose: when each sample names a purpose, every chosen purpose has one; when some samples name none, those samples cover the rest, one each. One sample needs STOP. The program name must be in each sample, ignoring case, spaces and dots. Link, phone-number and content attributes come from validated answers. Legacy brand-only snapshots missing the new evidence need correction before a new campaign filing; already-filed campaigns continue normal status recovery.
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

## Document check (AI read)

`POST /v1/verifications/document-check` (MCP `check_verification_documents`) reads the files of a number verification form with an AI model before submit. The console calls it once per uploaded file, only after the admin ticks the consent box and clicks "Use my document to fill in the fields". A file that stands for other slots (one passport for several proofs) is sent once, with `documents.<slot>.covers` naming those slots; one read fills all of them. It returns the values each file shows, by the carrier's field names, and warnings: unreadable, turned, expired, another document type, or a typed name that differs from the printed one. Hail words every warning; the model only reads. Every name it reads is the holder's: the person or company the document is about or addressed to, never the issuer (a bank, phone company, utility or authority). Code: [`document_check.py`](../../core/hailhq/core/providers/verification/document_check.py), [`document_reader.py`](../../core/hailhq/core/providers/llm/document_reader.py), [`document_reads.py`](../../core/hailhq/core/document_reads.py) (the cap).

- **Model:** `DOCUMENT_READER_MODEL`, code default `ANTHROPIC_BACKUP_MODEL` (Haiku 5.5). Not in `.env.example`: set it only to try another model. It must take effort `low` and structured output; its thinking-off value comes from `THINKING_OFF` ([`thinking.py`](../../core/hailhq/core/providers/llm/thinking.py)). Bedrock when `ANTHROPIC_BEDROCK_REGION` is set, else Anthropic's API ([voice-llm.md](voice-llm.md)).
- **Fallback models:** when another model may help (`_next_model_may_help` in `document_reader.py`: a missing model or one without access, a rate limit (set per model on Bedrock and Anthropic's API), a server or connection error, or a 400 or 424 whose text names a model problem, not the file), the same read tries `ANTHROPIC_BACKUP_MODEL`, then `ANTHROPIC_MODEL`, each id once, each with its own thinking-off value. Any other 400 or 424, or a 413, is about the file: the document goes to no other model. Never after a timeout, and only with at least 1 s left of the same 15 s. A fallback never uses a second read. Logs name the exception type and the try number only. Probed on 2026-11-16, Bedrock eu-central-1: Haiku 5.5 and Sonnet 5.5 (with `between_tools`) read a PNG and a PDF; Sonnet 5.5 with `disabled` is a 400. Probed on 2026-11-17: an unknown Bedrock id is a 400 "The provided model identifier is invalid." A model that refuses the request is harmless: the read fails open.
- **Cautions after an edit:** `POST /v1/verifications/document-cautions` (JSON, console only, no MCP tool) gives the `name`, `document_type` and `expired` warnings again for the form as typed and chosen now, from the reads the console got earlier (`documents.<slot>.read`, the whole object the check returned). Same rules as the check (`document_cautions` in `document_check.py`); no file, no model, no read used. The "does not look like one" type warning and the expiry need `none_of_types` and `expiry_date` in the read; slots with the same `file_slot` get one name check per box. The carrier's form is kept 10 minutes per API process for this route (`_caution_forms`), so an edit does not call the carrier. Requests that miss at once share one carrier call. A slot missing from a form kept 30 s or longer fetches the form once more before the 422. If the carrier is down for that fetch, the kept form answers, one warning line is logged, and the next try waits 30 s again. If the carrier refuses (a 4xx, like a subject type it no longer takes), the kept form is dropped and that refusal answers for 10 minutes, without another carrier call.
- **Fails open:** no model, an error on every model tried, a timeout, a refusal, a PDF of more than 5 pages, a file that is not its declared type, a PNG or progressive JPEG over 40 million pixels (larger baseline JPEGs are decoded at a reduced scale), more than 15 s, under 1 s left before the send, a failed bucket fetch, a file past the 30 MB total, or a failed cap reservation gives `read: false` for those slots and no warning; the other files are still read. The customer fills the form by hand. Only the first 3 pages of a PDF go to the model. Files are prepared in a child process without credentials ([`document_image.py`](../../core/hailhq/core/providers/llm/document_image.py), run by [`isolated_process.py`](../../core/hailhq/core/isolated_process.py), shared with the voicebot's attachment reader), one at a time per API process, with 512 MB of memory and 10 s each: the server has no swap, and live calls run beside the API. The model gets only what is left of the 15 s.
- **Cap:** 30 reads per workspace per UTC day (`DAILY_DOCUMENT_READS`). A read is one file sent to the model, whatever happens to the call after (an error, a timeout). A request reserves its reads before any file is fetched, so a workspace over the cap costs nothing; a reserved read whose file was never sent (not readable, over the limits, not decoded) is given back. With no model configured nothing is reserved or fetched. One file given for several slots (one upload, or one passport covering several proofs) is read once and counts once. Past the cap a slot is `read: false` with `skipped: "daily_limit"`, no error and no warning; the console shows "You have used today's document reads. Fill in the boxes below." A Haiku 5.5 read costs well under 1 cent. A read that falls back to Sonnet 5.5 costs more. A refused request or a missing model is not billed; a try that fails after the model answered in part can be. So the 30 reads bound the count, not a fixed price per day.
- **Count:** rows in `agent_send_log` with `channel = 'document_read'` and the slot name as `recipient`. Today's usage for a workspace:

  ```sql
  SELECT count(*) FROM agent_send_log
  WHERE organization_id = '<org id>' AND channel = 'document_read'
    AND created_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
  ```

- **Privacy:** the files go to the model provider for this read only. Logs hold counts and exception types, never a value, a name or the answer.

**Open decision: charge per read or keep free.** Reads are free to customers now; Hail pays the provider. Options:

1. Charge per read from credits. Needs a per-read price in the website rater (prices are kept out of this repo) and a usage row the rater prices.
2. Keep free, with the daily cap (today).
3. Free only for workspaces that hold credits; others fill the form by hand.

## Kept answers and files (resubmit)

A rejected number registration (`purpose=number`) can be fixed and filed again without typing everything again:

```bash
curl -X POST https://api.hail.so/v1/verifications/$ID/resubmit \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -F 'fields={"first_name":"Ada","last_name":"King"}' \
  -F 'file.proof_of_identity=@passport.jpg;type=image/jpeg'
```

What Hail keeps, from the moment of `POST /verifications`:

| What                                                        | Where                                                                                                                                                                                                                                                                                                              |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Typed answers (fields, address, document options, quote id) | `carrier_verifications.provider_refs.application` (the same key US texting uses). Encrypted with Fernet and `HAIL_PROVIDER_SECRET_KEY` under `sealed`; only `schema_version` and `quote_id` stay readable. Without that key (self-host), stored as plain JSON. If the key changes, the answers read as gone (404). |
| Each file                                                   | Mail bucket, `verification-kept/<org>/<upload id>`, written with S3 server-side encryption (`AES256`). Row in `file_uploads` with `purpose = 'verification_kept'`, `verification_id` and `slot`.                                                                                                                   |

When it is deleted (whichever comes first):

| Trigger                             | Answers                                                                  | Files                                                                                                                                                                                                 |
| ----------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 30 days after submit (`created_at`) | Background pass (`forget_kept_registrations`). Reads return 404 at once. | Background pass.                                                                                                                                                                                      |
| Carrier approves                    | At once (`_apply_status`).                                               | Right after that commit (`_refresh`); the background pass retries.                                                                                                                                    |
| Customer cancels (`DELETE`)         | At once (`_wiped_refs`).                                                 | Right after the cancel; the background pass retries.                                                                                                                                                  |
| Resubmit filed                      | Moved to the new row in the same commit.                                 | Files sent again move to the new row in the same commit (no copy), and each object is copied onto itself so the lifecycle clock restarts. The old row's other files are deleted after the new filing. |
| Account close                       | `POST /internal/org-closures` and every pass.                            | Same.                                                                                                                                                                                                 |
| Backstop                            | none                                                                     | S3 lifecycle rule `expire-verification-kept`: 32 days.                                                                                                                                                |

A rejection (carrier, carrier refusal of the draft, or superadmin) keeps both. The background pass runs every `HAIL_VERIFICATION_POLL_SECONDS` (600 s). A failed S3 delete keeps the row and is retried on the next pass. A failed S3 write at submit only logs (`could not keep the files of verification <id>`); the files written before it stay kept, the filing goes ahead, and the customer adds the missing files again on a resubmit. No value or file name is ever logged.

Resubmit rules ([`resubmit_verification`](../../api/hailhq/api/routes/verifications.py)):

- Only a `rejected` number registration whose answers are kept (`resubmittable: true`, `kept_until` on the verification). Else 409 or 404.
- `fields`, `address`, `documents` each replace the kept value whole. `file.<slot>` / `upload.<slot>` replace that slot's kept file; other slots reuse the kept file while they keep the document option it was filed with (a new option needs a new file). `quote_id` defaults to the kept quote. The quote purge (`purge_expired_quotes`) skips a quote a kept application names, so it works for the console's form and the resubmit for the 30 days. A 409 means that offer is no longer sold: quote again and pass the new one.
- `purpose`, `country_code`, `number_type`, `subject_type`, `number_id` cannot change (422). `provider` is ignored, as on `POST /verifications`.
- The new filing runs every check of `POST /verifications` (422, 402 credit hold, 409). Nothing changes on the old row unless the new one is saved. Then the old row is dismissed like `DELETE` (carrier draft discarded); if the carrier is down, it stays `rejected` with nothing kept.
- Two resubmits at once: the first to commit takes the answers; the other gets 409 and its carrier draft is discarded.
- `GET /verifications/{id}/application` returns the kept answers, file names (never the bytes), `quote_id` and `hold_cents` (what a resubmit sets aside), `Cache-Control: no-store`. MCP: `get_sms_application`, `resubmit_verification`.

Support resubmits for a customer with the same call, using an API key of the customer's workspace (the console uses "Fix and resubmit" on the rejected row). Find kept rows:

```sql
BEGIN READ ONLY;
SELECT v.id, v.organization_id, v.state, v.created_at + interval '30 days' AS kept_until,
       count(f.id) AS files
FROM carrier_verifications v
LEFT JOIN file_uploads f ON f.verification_id = v.id AND f.purpose = 'verification_kept'
WHERE v.purpose = 'number' AND v.provider_refs ? 'application'
GROUP BY v.id
ORDER BY v.created_at DESC;
ROLLBACK;
```

Infra: the API's IAM policy needs `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject` on `verification-kept/*` and `s3:ListBucket` with that prefix ([`infra/terraform/iam.tf`](../../infra/terraform/iam.tf)), plus the lifecycle rule in [`infra/terraform/s3_mail.tf`](../../infra/terraform/s3_mail.tf). Apply the Terraform stack before or with the deploy (`cd infra && terragrunt apply`). Until then, keeping files fails open (answers are kept, files are not).

## Cancel rules

`DELETE /verifications/{id}`:

- `awaiting_review` → `cancelled`, draft discarded at the carrier.
- `rejected` → `cancelled` (dismiss), draft discarded. Exception for a Telnyx person (`purpose=sms`) whose PIN was confirmed and whose campaign was filed: only the campaign is deleted, the brand stays at Telnyx, and the cancelled row holds `provider_refs.kept_brand_id`. The next filing reuses that brand when Telnyx still shows it confirmed and first name, last name, email and mobile match: no new brand, no PIN, `provider_refs.brand_reused`, no submission fee. A kept brand Telnyx answers 404 or 410 for is gone; any other error is a retryable 503, so an auth or account error never buys a second brand. Otherwise it files a new brand, or returns 402 with no brand when the credit does not cover one. The kept brand then stays at Telnyx, as does the brand of a customer who never files again: Telnyx refuses to delete a brand for 3 months after its campaign.
- `submitting`, `submitted` → **409** while review is pending. Exception: an SMS campaign already accepted by the carrier but waiting in `runtime_setup` (`campaign_accepted=true`, `runtime_ready=false`) can be cancelled. The API rates the existing campaign before discarding it and rechecks eligibility under locks, preserving normal billing and provider cleanup behavior. A `needs_information` registration is repaired with PATCH on its existing row, not cancellation. Every verification response carries `cancellable`: true when `DELETE` accepts the row now. Clients show Cancel from it.
- `approved` with `purpose=sms` → `cancelled`. Each number on it (`phone_numbers.sms_registration_id`) leaves the campaign (Telnyx unlink; a person's Twilio number leaves the person's service and is set up in the org's service again), then the campaign, brand, bundle and profile are discarded. `cancelled_at` is set; the website rater bills the monthly fee through that month. Telnyx bills at least 3 monthly fees from approval, so for Telnyx the rater bills through the later of the cancel month and the approval month + 2 (`minimum_months` in `costs/sms-registration.json`). Audit row `verification.cancel`.
- `approved` with `purpose=number`, `cancelled` → 409.
- A toll-free registration whose number was moved into the trial texting pool → 409, and an account close keeps it (`close_refused`): see [sms-trial-numbers.md](sms-trial-numbers.md#the-moved-numbers-old-registration).
- Carrier delete failed. Every plug-in's `discard` runs all its steps, then raises ([`DiscardFailures`](../../core/hailhq/core/providers/verification/base.py)); a record the carrier no longer holds (404, 410) counts as deleted. An error that escapes `discard` outside its steps counts the same way ([`counted_discard`](../../core/hailhq/core/providers/verification/base.py), on every discard: customer cancel, account close, background pass, superadmin reject). Any httpx or requests transport error (a lost connection, a timeout, a response cut off in transit) is an outage. The error nearest the one raised decides: a lost connection while handling a 403 is an outage. A number's unlink from an SMS registration (`detach_number`) counts the same way: a 404 is done, an outage answers 503, and a refusal lets the cancel go through with the number in `discard_refused.unlinks`; at close it follows the close column. An error outside the carrier delete (the database) answers 500 on the route and is retried with no limit at close. How a failure counts is one table, [`failure_kind`](../../core/hailhq/core/carrier_routing.py):

  | Failure                                                                   | `DELETE /verifications/{id}`                                                                  | Account close                             |
  | ------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----------------------------------------- |
  | 5xx, timeout, lost connection, 401, 408, 425, 429, carrier not configured | **503** `carrier unavailable; try again later`. Nothing changes: state, carrier ids, numbers. | Retried every pass, no limit.             |
  | 403, 409 (the carrier refuses this record for now)                        | Cancelled in Hail, ids kept in `provider_refs.discard_refused`.                               | Retried for 7 days, then `close_refused`. |
  | Any other 4xx (400, 422)                                                  | Cancelled in Hail, ids kept in `discard_refused`.                                             | `close_refused` at once.                  |
  | A step that failed with no carrier answer and no network error (a bug)    | Cancelled in Hail, ids kept in `discard_refused` (`status` null).                             | Retried for 7 days, then `close_refused`. |

  On a 503, the numbers of an approved SMS registration may already have left the campaign at the carrier (that runs before `discard`); a retry is safe, as both steps accept work already done. A cancel that goes through on a refusal stops Hail's fees like any cancel (`cancelled_at`; `_notify_billing`). A draft that failed while it was made is cleaned up best effort (`discard_draft`), so the error that stopped it is the one reported.

**`discard_refused`** (`{"status": <HTTP status or null>, "at": <ISO time>, "held": {<carrier ids>}}`): the row ended in Hail (`cancelled`, or `rejected` by the background pass or a superadmin) while the carrier still holds a record. `held` has the carrier ids and flags, never the customer's answers. When the carrier refused to unlink a number, `unlinks` lists each one (`number_id`, `provider_resource_id`, `status`): unlink it at the carrier by hand. `status` is the delete's, else the first unlink's. The mark stays when a rejected row is dismissed. Ops deletes what `held` names at the carrier by hand, then removes the key. Find them:

```sql
BEGIN READ ONLY;
SELECT id, organization_id, provider, purpose, state, updated_at,
       provider_refs->'discard_refused' AS discard_refused
FROM carrier_verifications
WHERE provider_refs ? 'discard_refused'
ORDER BY updated_at DESC;
ROLLBACK;
```

At account close ([`hailhq.api.org_close`](../../api/hailhq/api/org_close.py), not a route): every row that is not `cancelled` is cancelled, in any state, with the whole filing discarded (no brand kept). A `cancelled` row with `provider_refs.kept_brand_id` has that brand discarded and the key dropped. A row waits while a number it covers is not released yet (a number verification: the org's numbers of its carrier, country and type; an SMS registration: the numbers on it and its toll-free number). When the carrier refused that number's release (`release_refused`), the row is not cancelled: it is marked `close_refused` with `status` null and the `number_id`, its ids kept for the delete by hand. The close does not call the fee rater and writes no audit row. A failure counts as in the table above. A refusal is stored as `provider_refs.close_refused` (`status`, `at`) and the row is not tried again, with its state and carrier ids. The 7-day window starts at the first 403, 409 or bug (`provider_refs.close_failing_since`; `provisioning_metadata.close_failing_since` on a number); a success or any failure that does not count clears it, so it only measures refusals that keep coming. A closed org's numbers that leave a person's Twilio campaign are not set up for SMS again. Left at the carrier after a close: a Twilio texting brand (no delete API), and a Telnyx brand within 3 months of its campaign. The background pass submits, polls and attaches nothing for a closed org. Find what a close left at the carrier:

```sql
BEGIN READ ONLY;
SELECT id, organization_id, provider, purpose, state,
       provider_refs->'close_refused' AS close_refused
FROM carrier_verifications
WHERE provider_refs ? 'close_refused'
ORDER BY organization_id;
ROLLBACK;
```

## Superadmin routes

`/admin/verifications` (list, `POST /{id}/approve`, `POST /{id}/reject`) are not in the OpenAPI spec. Only a console session the website minted with `superadmin: true` passes ([`api/hailhq/api/superadmin.py`](../../api/hailhq/api/superadmin.py)); API keys never do. The console page lives in hail-website at `app/console/admin/verifications/`. Approve re-checks the draft with the carrier, then submits; reject records the reason and discards the draft. If the row changed while a filed campaign was rated (the background pass submitted it, say), reject answers 409 and discards nothing. If the discard fails on an outage, reject answers 502 and the row keeps its state and ids; if the carrier refuses the delete, the row is rejected with `discard_refused`. Both write an audit row (`verification.approve`, `verification.reject`) with the admin as actor.

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
for 10 minutes in each API process, so a revoked approval, or a number moved
out of the Twilio account, stops sends within 10 minutes. A "no" is not kept. This check writes no provider or
Hail registration and stores no guessed customer identity. Other carriers keep
their existing carrier-side checks until Hail supports their TFV API.

Official guidance checked November 14, 2026:
[approval guidance](https://www.twilio.com/en-us/blog/insights/best-practices/improving-your-chances-of-toll-free-verification-approval),
[API onboarding](https://www.twilio.com/docs/messaging/compliance/toll-free/api-onboarding),
[verification API](https://www.twilio.com/docs/messaging/api/tollfree-verification-resource).
The SDK regression tests use the pinned real SDK with an offline HTTP recorder.
No live provider filing, message, charge or refund is part of local validation.

## Rejection reasons

A number verification the carrier rejects gets a `rejection_reason` the customer can act on.
The console, the rejection email, the API and the MCP server all show it.
The plug-in builds it in `status()` with [`rejections.py`](../../core/hailhq/core/providers/verification/rejections.py):

- **Twilio.** The bundle holds no reason. Each rejected supporting document has `failure_reason` and `errors[].code`, and the newest evaluation lists failed fields. A known code gets Hail's fix (`TWILIO_FIXES`, from Twilio's 18001-18064 error list). Any other code shows Twilio's text after "The carrier said:". Some rejections carry no reason at all; then the generic line stays.
- **Telnyx.** The requirement group lists each `declined` or `expired` requirement. Hail names it from `GET /requirement_types/{id}`.
- **DIDWW.** DIDWW rejects the registration when a number is ordered. The order step (`rejected_registration` in `api/hailhq/api/number_orders.py`) puts the address verification's `reject_reasons` and `reject_comment` after "The carrier said:".

The carrier's text can quote what the customer filed. It is shown to the workspace (console, API, MCP) and in the rejection email to the workspace owners, sent through the email provider. It is kept in the audit row `verification.reject` (`carrier_reason`), and never logged.
