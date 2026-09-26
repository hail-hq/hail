# DIDWW as a purchasable carrier: quotes, registration, orders

Status: draft for review. Date: 2026-09-26.

## Problem

DIDWW numbers work for outbound calls (`docs/public/self-host/didww.md`), but a customer cannot buy one. An operator buys the number in the DIDWW panel, registers the end user there, and inserts the `phone_numbers` row by hand. DIDWW sells number types Twilio does not (Portugal national, for one), so the console shows offers nobody can complete.

## Goal

- `POST /numbers/quotes` returns DIDWW offers next to Twilio and Telnyx. The existing ranking picks the best one.
- A customer who needs registration fills the existing verification wizard. Hail validates the papers with DIDWW before any purchase.
- `POST /numbers` buys the number at DIDWW, files the registration, and activates the number when DIDWW approves.
- Adding DIDWW touches the carrier registry, two provider modules, and the order reconciler. No console change.

## Non-goals

- SMS through DIDWW. Offers advertise `voice` only. `enable-sms` keeps refusing DIDWW numbers.
- Inbound calls.
- Console changes. The wizard and the number picker already speak the neutral models.
- DIDWW emergency calling, capacity pools, trunk groups.

## Decisions (agreed with the product owner)

1. Every country and number type DIDWW sells with `voice_out`, not only Portugal.
2. DIDWW competes in the same quote ranking as Twilio and Telnyx. No carrier preference.
3. Order of operations is what DIDWW allows: validate papers, buy, file the registration, activate on approval. DIDWW cannot approve papers before a DID is owned (`address_verifications` requires `dids`).
4. If DIDWW rejects the registration after purchase, the customer gets the monthly fee back and keeps paying the setup fee. Hail terminates the DID.
5. The `didww` Python SDK (PyPI, MIT, sync) does the HTTP and the file encryption. Calls run in `asyncio.to_thread`, as the Twilio adapter does.

## Architecture

```
POST /numbers/quotes ──> discover_offers ──> didww_offers (available_dids + did_groups + address_requirements)
console wizard ──> /verifications ──> VerificationProvider "didww" ──> identities / addresses / proofs
                                                                   ──> address_requirement_validations
POST /numbers ──> acquire_offer ──> place_didww_order (POST /orders)
reconciler ──> didww_order_outcome ──> GET /dids?filter[order.id] ──> POST /address_verifications ──> poll
                                   ──> active | failed (terminate DID, partial refund)
```

## Flow and states

1. **Quote.** `didww_offers()` runs next to `twilio_offers` and `telnyx_offers` inside `discover_offers`. Offers carry the live price and `readiness`.
2. **Papers.** When the offer says `verification_required`, the customer fills the wizard. The DIDWW plug-in creates identity, address and proofs at DIDWW and runs `POST /address_requirement_validations`. A 422 becomes field problems and the draft is discarded. Success makes the Hail verification row `approved`, meaning "papers valid, ready to buy". A superadmin still approves the row as today.
3. **Order.** `acquire_offer` reserves setup + first month, inserts the number as `pending`, and calls `place_didww_order`: `POST /orders` with one `did_order_items` entry (`available_did_id`, `sku_id`, `allow_back_ordering=false`). No DID reservation step: a number that vanished between quote and order is a 409, like Twilio. The order id is stored in `provisioning_metadata.order_id`.
4. **Outcome.** `Carrier.async_orders=True` for DIDWW, so the reconciler polls `didww_order_outcome` every `ORDER_POLL_INTERVAL`:
   - order `canceled` → `failed`.
   - order `pending` → `pending`.
   - order `completed`: find the DID with `GET /dids?filter[order.id]=<order>` (include `address_verification`).
     - `awaiting_registration=false` → `active`, `provider_resource_id` = DID uuid.
     - `awaiting_registration=true` and no `address_verification` → create one (`address` = the approved handle's address id, `dids=[DID]`, `service_description` from the wizard when required). Store its id in `provisioning_metadata.verification_id`. Return `pending`.
     - verification `pending` → `pending`; `approved` → `active`; `rejected` → `rejected_registration`.
   - Idempotency guard: the DID's `address_verification` relationship. A verification is never created twice.
5. **Settle.** `finish_order` as today for `active` and `failed`. New outcome `rejected_registration`: state `failed`, `PATCH /dids/{id}` `terminated=true`, credits back for the monthly fee only, the setup debit stays.
6. **Timeout.** New `Carrier.pending_timeout`: Twilio 2 h, Telnyx 2 h, DIDWW 7 days. `reconcile_order` reads it in all three timeout branches instead of the module constant.
7. **Release.** `DELETE /numbers/{id}` → `PATCH /dids/{id}` `terminated=true`. DIDWW stops renewal at the end of the billing cycle. No refund from DIDWW.

## Discovery: `didww_offers`

- `GET /available_dids` filtered by `country.id` and `did_group_type.id`, `include=did_group.stock_keeping_units,did_group.did_group_type`. Up to 3 numbers, like Twilio. With `e164` set, filter by number.
- Country and type ids come from `GET /countries?filter[iso]` and `GET /did_group_types`, cached in memory.
- Keep only groups whose `features` contain `voice_out`. A number that cannot be a caller ID is useless to Hail.
- `capabilities = ["voice"]` always.
- Type map from `did_group_type.name`: Local → `local`, National → `national`, Mobile → `mobile`, Toll-free → `toll_free`. Anything else is skipped.
- Price: the SKU with `channels_included_count == 0` (metered). `setup_cents`/`monthly_cents` from it. No such SKU → no offer.
- Readiness:
  - `needs_registration=false` → `ready`, friction `none`.
  - else an approved Hail verification for (org, `didww`, country, type) exists → `ready`, `verification_id` = identity id, `address_id` = address id.
  - else `verification_required`; friction `documents` when `personal_proof_qty`, `business_proof_qty` or `address_proof_qty` > 0, else `information`. `requirements` labels list the proof types and mandatory fields.
- Offers return one DIDWW number per request (like Telnyx). `restriction_message` is not shown (the neutral model has no help field). `verification_id` and `address_id` both carry the approved address id.
- The catalog gate `is_acquirable(country, type)` applies to Twilio and Telnyx only. DIDWW offers carry their own live price, and the renewal rater bills the price stored on the number, so DIDWW countries need no `costs/telephony.json` row.
- The approved registration is found at DIDWW, like the other carriers find theirs: `GET /addresses?filter[external_reference_id]=hail:<org>:<country>:<number_type>`. Nothing moves out of `api/routes/verifications.py`.

## Verification plug-in: `providers/verification/didww.py`

- Registered as `"didww"` when `DIDWW_API_KEY` is set.
- `requirements(country, number_type, subject_type)`: `GET /address_requirements?filter[country.id]&filter[did_group_type.id]` with proof types included.
  - `identity_type` `personal`/`business`/`any` → allowed subject types. A subject type outside it raises `UnsupportedSubjectType`.
  - `personal_mandatory_fields` / `business_mandatory_fields` → `fields` (first name, last name, birth date, id number, company name, registration number, VAT id, personal tax id, phone, email). Labels are Hail's; keys are DIDWW attribute names.
  - `personal_proof_types` or `business_proof_types` with quantity N → N identity document slots, each with one option per proof type, `file_required=True`.
  - `address_proof_types` with quantity N → N address document slots, `needs_address=True`.
  - `address_area_level` `country`/`area`/`city` → `address_required=True`. `world_wide` → address still collected (DIDWW needs an address object), but no location constraint.
  - `service_description_required` → one extra text field `service_description`.
  - `restriction_message` → `help` on the requirements.
  - No requirement row → `required=False`.
- `create_draft`: identity (`external_reference_id=hail-<org>`, `contact_email` = `HAIL_SUPPORT_EMAIL`), address (country id, city name, postal code, address line), one encrypted file per document (public keys from `GET /public_keys`, SDK `Encrypt.calculate_fingerprint` + `encrypt_with_keys`, fixed file name), one proof per slot linking the file to the identity or the address, then `POST /address_requirement_validations`. 422 → `Problem`s, everything created is deleted. `refs = {identity_id, address_id, proof_ids, file_ids, requirement_id, service_description}`.
- `check(refs)`: re-run the validation. `submit(refs)`: `PATCH /addresses/{address_id}` setting `external_reference_id` to `hail:<org>:<country>:<number_type>` (the draft carries `hail-draft:…`). `status(refs)`: `approved` when the address carries the `hail:` reference, else `draft`. `purchase_handle(refs)`: `{identity_id, address_id}`. `discard(refs)`: delete proofs, files, address, and the identity when this draft created it; best effort.
- One identity per organization at DIDWW. A second verification for another country reuses the identity when its country matches, otherwise creates a new one.
- Hail stores only ids. Documents go straight to DIDWW encrypted with DIDWW's keys.

## Code touch points

- `core/hailhq/core/providers/voice/didww.py` (new): `DidwwClient` factory, `didww_offers`, `place_didww_order`, `didww_order_outcome`, `release_didww_number`, `terminate_did`. Header `X-DIDWW-API-Version: 2026-04-16`.
- `core/hailhq/core/providers/verification/didww.py` (new).
- `core/hailhq/core/carrier_routing.py`: `CARRIERS[DIDWW]` gets `async_orders=True`; new `Carrier.pending_timeout: timedelta`.
- `core/hailhq/core/carrier_offer.py`, `core/hailhq/core/schemas.py`: `provider` Literals gain `"didww"`.
- `core/hailhq/core/number_offers.py`: `PROVIDERS`, `searches["didww"]`; per-carrier catalog gate.
- `api/hailhq/api/number_orders.py`: `carrier_outcome` dispatches on carrier name; new outcome `rejected_registration` and its partial refund; timeouts read `carrier(...).pending_timeout`.
- `api/hailhq/api/routes/numbers.py`: `_RELEASERS[DIDWW] = release_didww_number`.
- `core/hailhq/core/config.py`: `didww_api_key`, `didww_environment` (`production` | `sandbox`, default `production`).
- `.env.example` and the local `.env`: `DIDWW_API_KEY=`, `DIDWW_ENVIRONMENT=production`.
- `core/pyproject.toml`: dependency `didww` (MIT).
- `openapi/openapi.yaml` regenerated (prettier), CLI `make codegen`.
- `docs/public/self-host/didww.md`: purchase flow replaces the manual SQL; API key setup.

## Error handling

- DIDWW unreachable during quotes → carrier listed in `unavailable_providers`, no offer. Never a zero price.
- DIDWW 4xx on order (other than 408/409) → `failed`, full refund, like Twilio. 5xx or timeout → stays `pending` for the reconciler.
- Reconciler lookup errors → retried until `pending_timeout`, then failed with refund and an operator log line naming the order.
- Rate limit 429 → treated as a transient lookup error.
- Validation 422 → field problems shown in the wizard; nothing is left behind at DIDWW.

## Testing

- Unit tests mock at the `requests` layer with `responses`, as the Twilio adapter does.
- `didww_offers`: voice_out filter, SKU choice, type map, readiness with and without an approved handle, unavailable carrier.
- `didww_order_outcome`: table test over order status × `awaiting_registration` × verification status, including the create-once guard.
- Reconciler: 7-day timeout for DIDWW, 2 h unchanged for others; `rejected_registration` refunds monthly only and terminates the DID.
- Verification plug-in: requirement mapping for a personal and a business requirement; `create_draft` happy path; validation 422 → problems and cleanup; `discard`.
- Release: `terminated=true` call.
- One manual smoke run against the DIDWW sandbox (`DIDWW_ENVIRONMENT=sandbox`, key from the Sandbox User Panel → API → DIDWW API 3) before the first production purchase.

## Privacy

Same rule as the carrier verification spec: Hail stores ids and states only. Files are encrypted to DIDWW's public keys in memory and sent once. The identity's contact email is Hail's support address, never the customer's.
