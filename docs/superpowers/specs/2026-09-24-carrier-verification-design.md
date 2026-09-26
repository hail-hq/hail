# Carrier verification: collect documents in the console, build the carrier's compliance record automatically

Status: draft for review. Date: 2026-09-24.

## Problem

Some countries (the UK is the first case) require a number holder to be verified with the carrier before a number can be bought. Today an operator collects the documents by email and builds the carrier's compliance record by hand in the carrier's console. `POST /numbers` on `main` cannot buy such a number without it (see hail-hq/hail#110 for the stopgap).

## Goal

- A customer verifies their organization inside the hail console.
- Hail builds the carrier-side record through the carrier's API.
- A hail superadmin approves it before it is submitted to the carrier.
- Once approved, the customer buys numbers that need it.
- Adding a carrier later means adding one plug-in. The API, database and UI do not change.

## Non-goals

- Hail does not store identity documents or personal details. See Privacy.
- No new carrier in the first version except Twilio.
- The superadmin role itself. It is built separately. This spec only depends on one check for it.
- SMS sender registration (10DLC, alphanumeric IDs). Different process.

## Decisions (agreed with the product owner)

1. Documents go straight to the carrier. Hail keeps nothing.
2. A superadmin approves each verification before it is submitted.
3. Carrier-neutral. Twilio-specific code exists only inside the Twilio plug-in.
4. First version covers every country and number type the carrier requires verification for. The form is built from the carrier's own requirement data, so a new country needs no code.
5. Approval uses a `superadmin` role that is added separately. No email allow-list.

## Architecture

```
console wizard ──> API /verifications ──> VerificationProvider (registry) ──> carrier plug-in ──> carrier API
                         │
                         └── carrier_verifications (states + opaque refs only)
POST /numbers ──> provider.purchase_handle(verification) ──> carrier purchase call
```

### 1. Neutral requirements schema

`VerificationProvider.requirements(country, number_type, subject_type)` returns:

- `fields`: list of `{name, type, label, help, pattern, required}` for the person or business.
- `documents`: list of `{name, label, accepted_types, min, max, file_types}`. Each entry is a slot the customer fills with a file.
- `address`: `{required, allowed_countries}`.
- `subject_types`: which of `person` and `business` the carrier accepts.
- `version`: a hash of the schema, stored with the verification.

Nothing in this shape names a carrier concept such as a bundle or an end user.

### 2. Provider interface

```
class VerificationProvider(ABC):
    name: str
    async def requirements(country, number_type, subject_type) -> Requirements
    async def create_draft(org_id, country, number_type, subject_type,
                           fields, address, files) -> DraftResult   # refs + problems
    async def check(refs) -> list[Problem]        # per-field problems, empty if ready
    async def submit(refs) -> None
    async def status(refs) -> ProviderStatus      # pending | approved | rejected(reason)
    async def purchase_handle(refs) -> dict       # opaque kwargs for the carrier's purchase call
    async def discard(refs) -> None
```

A registry maps `provider` name to its implementation. The number's carrier decides which one is used.

### 3. Twilio plug-in

Maps the interface to Twilio Regulatory Compliance:

- `requirements`: reads Regulations for the country, number type and subject type. Converts end-user fields, document types and address requirements to the neutral schema.
- `create_draft`: creates an Address, an EndUser, SupportingDocuments (file bytes go to Twilio's upload host), a Bundle, and ItemAssignments. Returns their SIDs as `refs`.
- `check`: runs an Evaluation and converts failures into `Problem(field, message)`.
- Contact email: `create_draft` receives `contact_email`, the operator's address from the general `HAIL_SUPPORT_EMAIL` setting (default `hi@hail.so`). The plug-in gives it to the carrier wherever the carrier needs a contact for notices (Twilio: the bundle's `email`). It is never the customer's; the customer's email goes only in the fields that describe them.
- `submit`: sets the Bundle status to pending review.
- `status`: reads the Bundle status. `twilio-approved` is approved. `twilio-rejected` is rejected with Twilio's reason.
- `purchase_handle`: returns the Bundle SID for the purchase call. The lookup by name `hail-<organization_id>` from #110 becomes a fallback in here.
- Number-type names, statuses and SIDs are converted inside this plug-in only.

### 4. Data: table `carrier_verifications` (migration `0044`)

| column                                            | type        | note                                                                                       |
| ------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------ |
| id                                                | uuid pk     |                                                                                            |
| organization_id                                   | uuid fk     |                                                                                            |
| provider                                          | text        | registry key                                                                               |
| country_code                                      | text        |                                                                                            |
| number_type                                       | text        |                                                                                            |
| subject_type                                      | text        | `person` or `business`                                                                     |
| state                                             | text        | `draft`, `awaiting_review`, `submitting`, `submitted`, `approved`, `rejected`, `cancelled` |
| provider_refs                                     | jsonb       | opaque carrier IDs. No personal data                                                       |
| requirements_version                              | text        |                                                                                            |
| rejection_reason                                  | text null   | carrier or superadmin text                                                                 |
| approved_by                                       | uuid null   | superadmin user id                                                                         |
| created_at, updated_at, submitted_at, approved_at | timestamptz |                                                                                            |

Partial unique index: one live verification (not `cancelled`, not `rejected`) per (organization, provider, country, number_type).

Not stored: names, emails, phone numbers, addresses, document files, document numbers.

### 5. State machine

```
draft ──(check passes)──> awaiting_review ──(superadmin approve)──> submitting ──> submitted ──(carrier)──> approved
  │                              │                                        └────────────────> rejected
  │                              └──(superadmin reject)──> rejected
  └──(customer cancel)──> cancelled
```

- `rejected` allows a new verification for the same key (the old row keeps the reason).
- `submitted` → `approved`/`rejected` is updated by polling `provider.status` (on read, plus a periodic task, same pattern as `reconcile_pending_orders`).

### 6. API (hail-hq/hail, `api/hailhq/api/routes/verifications.py`)

Org routes (org admin role):

- `GET /verifications/requirements?country&number_type&subject_type`
- `POST /verifications` multipart: JSON `fields` and `address`, plus files by slot name. Calls `create_draft`, then `check`.
  - Problems: `422` with `[{field, message}]`. The draft is discarded so nothing is left at the carrier.
  - Ready: row is `awaiting_review`. Returns `201` with the id and state.
- `GET /verifications`, `GET /verifications/{id}`
- `DELETE /verifications/{id}` cancels and calls `discard`.

Superadmin routes (behind `require_superadmin`):

- `GET /admin/verifications?state=awaiting_review`
- `POST /admin/verifications/{id}/approve` calls `submit`, state `submitted`.
- `POST /admin/verifications/{id}/reject` with a reason.

`require_superadmin` is one dependency. Until the role exists it denies everyone.

### 7. Purchase

Only an `approved` verification is used, and the purchase route never calls the carrier to find out: it runs under an advisory lock and must not wait on or commit around a carrier call. A submitted verification becomes approved when the customer (or the periodic poll) reads it.

`POST /numbers`:

1. Find the org's `approved` verification for (provider, country, number_type).
2. If the carrier requires one and none exists: `422` "this number needs verification", plus the id of an in-progress one if any. No charge.
3. Call `provider.purchase_handle(refs)` and pass the result to the carrier purchase.

The existing debit order does not change: no debit before the carrier call succeeds.

### 8. Console (hail-website)

- Numbers page: a number that needs verification shows "Verify to buy" and opens the wizard. Buy is enabled only with an approved verification for that country and type.
- Wizard, built from `requirements`:
  1. Person or business.
  2. Fields (rendered from the field list, with the carrier's help text).
  3. Address, if required.
  4. Document slots with file pickers (10 MB, JPG, PNG, PDF).
  5. Review and submit. Problems from the API show next to their fields.
- Verification status page: awaiting review, submitted, approved, rejected with the reason and a start-again button.
- Superadmin page `/console/admin/verifications`: list of `awaiting_review` with Approve and Reject. Gated by `requireSuperadmin()`, which denies everyone until the role exists. Nothing on the page shows document contents; only the state, the org and the carrier.

### 9. Privacy

- Files and fields go browser → console server action → API → carrier in memory. They are never written to the database or logs, and hail's own code never saves them to disk. The web framework may spool a large upload to a temporary file while the request runs; that file is deleted when the request ends. Request bodies for these routes are excluded from request logging.
- Trade-off: files reach the carrier when the customer submits, before superadmin approval. The carrier holds them as a draft. Approval submits the draft for the carrier's review.
- `discard` runs on cancel and on a failed check so drafts are not left at the carrier.
- The reviewer sees no documents in hail. If the reviewer needs to look at them, they open the carrier's console.

### 10. Errors

- Carrier field problems are mapped to `{field, message}` and shown in place.
- Carrier outage on create: `503`, no row left behind.
- Carrier rejection: state `rejected` with the carrier's text shown to the customer.
- Superadmin rejection: same state with the superadmin's text.

### 11. Testing

- Provider: Twilio plug-in tested at the HTTP boundary with `responses`, like `test_twilio_voice.py`. Cases: requirements mapping for one individual and one business regulation, draft create, check failures, submit, status mapping, discard.
- A fake provider in the API tests proves the API and purchase path do not depend on Twilio.
- API: state machine transitions, one live verification per key, superadmin gate denies by default, purchase 422 without verification, no debit on failure.
- Console: wizard renders from a sample requirements payload, shows field problems, disables Buy until approved. Superadmin page hidden for non-superadmin.

## Build order

1. API: table, neutral schema, interface, registry, Twilio plug-in, org routes, purchase change.
2. Console: wizard and status page.
3. Superadmin routes and page (after the role exists).
4. Later, per carrier: a plug-in and its tests. Nothing else changes.

## Open items

- Superadmin role interface (owner: product owner). The two checks are the only coupling.
- Whether `submitted` state polling runs in the API process or a separate job.
- Migration number: `main` is at `0043`, but branch `codex/telnyx-discovery` already uses `0044` and `0045`. Renumber at implementation time to whatever is free when the work starts.
- Retention: a `cancelled` row's `provider_refs` are cleared on cancel.
