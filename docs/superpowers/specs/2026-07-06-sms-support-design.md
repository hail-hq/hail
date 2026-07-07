# SMS support — outbound, inbound, Sender ID, and pricing

Status: approved
Owners: r13i
Repo: `hail/` (core/api/cli/sdk/mcp/openapi) + `hail-website/` (console UI + billing). One spec; outbound and inbound share the same provider/model.

> **Revision (2026-07-06):** reconciled against a parallel compliance
> workstream that landed a generic `suppressions` table, `enforce_consent()`,
> and `compliance_gate.py` while this spec was in review. The original draft
> proposed a standalone `SmsSuppression` table without awareness of that
> work — this revision drops that duplication in favor of extending the
> shared system, generalizes the number-acquisition flow to be cross-channel
> rather than SMS-only, and adds an abuse-monitoring section for a
> confirmed gap (no automated opt-out/complaint-rate enforcement exists
> anywhere today — see "Abuse monitoring" below).
>
> **Revision (2026-07-07):** verified the shared-Brand/Campaign question
> (previously Risk 1, "unconfirmed") directly against Twilio's live API and
> official docs. Confirmed answer: Twilio does **not** support a fee-free
> shared campaign across unrelated end-customer businesses — their
> documented, recommended model is one subaccount + Brand + Campaign **per
> org** (Twilio's "Architecture #1"), and explicitly flags the pooled
> single-campaign model this spec uses as carrying real compliance exposure
> (one org's bad traffic can get the whole shared registration, and every
> other org's numbers riding on it, throttled or suspended by carriers).
> **Decision: accept this as a known, documented v1 risk rather than build
> full per-org registration now** — orgs still get real dedicated numbers
> and send/receive real SMS via the API as designed; only the underlying
> carrier-facing 10DLC registration is Hail's single identity, not each
> org's. See Decision 2 and Risk 1 below for the reasoning and the
> concrete mitigation this implies (abuse monitoring becomes load-bearing,
> not optional). Per-org registration (Architecture #1) is real,
> scoped future work — see "Future work."

## Goal

Add SMS as a first-class channel alongside voice and email: send and receive
text messages through Twilio, support a platform default Sender ID ("HAIL")
plus per-org customization where carriers allow it, and price it profitably.
SMS has been anticipated throughout the codebase since day one (the
`usage_events.channel` check constraint, `PhoneNumber.capabilities`, the
`private-rates.ts` rate table, the README milestones) but never implemented —
this spec closes that gap.

## Decisions (locked)

1. **Provider: Twilio, not AWS SNS.** SNS is publish-only — it cannot receive
   inbound SMS at all, which disqualifies it regardless of cost given inbound
   is a hard requirement. Twilio is also already the voice carrier (shared
   account, and `PhoneNumber`/`TwilioVoiceProvider` already model an `sms`
   capability).
2. **Architecture: one Twilio Messaging Service per org**, linked to a single
   Hail-owned platform-level A2P 10DLC brand + campaign. Twilio handles sender
   selection, sticky-sender (same number replies to the same contact), and
   inbound webhook routing per Messaging Service — this offloads logic that
   would otherwise have to be hand-rolled in `core/providers/sms/`.
   **Known, accepted tradeoff** (confirmed against Twilio's live account and
   docs, 2026-07-07): this pools org-branded messaging under one carrier
   registration, which Twilio explicitly documents as carrying real
   compliance exposure — see Risk 1. Chosen anyway for v1 to avoid per-org
   registration cost (~$1.50–10/mo + ~$20–60 one-time per org) and lead time
   (~2–3 weeks per org, dominated by Twilio's 10–15 day campaign review) that
   Architecture #1 (the fully-isolated, Twilio-recommended per-org model)
   would impose on every single org before they could send their first SMS.
   This is why the abuse-monitoring guardrail below is load-bearing, not a
   nice-to-have: it's the actual mitigation for the accepted risk.
3. **Geography: US and international from day one** — not a phased rollout.
4. **Sender ID is outbound-only and international-only.** A literal
   alphanumeric ID (default `"HAIL"`, org-customizable) is never used for
   US/Canada-bound messages (carriers there don't support it, and it has no
   inbound path). US/Canada sends always go out from the org's dedicated
   number.
5. **Custom per-org alphanumeric Sender ID is scoped to no-pre-registration
   corridors only** (e.g. Germany/most EU). Countries requiring registration
   with lead time (Australia — including its new industry-wide ACMA Sender ID
   Register effective July 2026; UK "protected" names) fall back to the
   platform default `"HAIL"`, registered once by Hail and reused across all
   orgs. India is excluded from alphanumeric sending entirely for v1 — Twilio
   silently overwrites alphanumeric IDs there with a random short code rather
   than displaying the brand name, so the feature wouldn't work as advertised.
6. **Two-way / US / Canada SMS requires a dedicated number on the org** — not
   the shared voice pool, since inbound replies need unambiguous number→org
   routing and 10DLC ties numbers to a registered campaign. An org's existing
   dedicated voice number is reused if present; pool-only orgs are blocked
   from US/Canada SMS (send and receive) with a clear error directing them to
   get a dedicated number.
7. **A minimal self-serve dedicated-number flow is in scope, built as a
   generic cross-channel capability — not an SMS-only feature.** Numbers are
   a shared resource across voice, SMS, and future MMS, so "acquire a
   dedicated number" and "toggle a capability on a number" are org-level
   number-management actions, not something bolted onto the SMS surface.
   Today numbers are pool-only with no purchase UI; this project builds the
   smallest generic version needed to unblock SMS, in a shape that voice and
   (later) MMS reuse without rework.
8. **Suppression and consent reuse the existing generic compliance system**
   (`Suppression` table, `enforce_consent()`, `compliance_gate.py`) rather
   than introducing SMS-specific equivalents — see "Architecture & data
   model" below.
9. **Inbound delivery is webhook-only** — no separate "forward to email/phone"
   mechanism. The org's existing `WebhookSubscription`/fanout mechanism
   (already used for email) is the sole inbound delivery path.
10. **Opt-out (STOP/HELP) state is tracked in Hail's own DB and exposed via
    API**, not left solely to Twilio's carrier-side Advanced Opt-Out. Outbound
    sends are blocked at the app layer if the destination has opted out.
11. **MMS is deferred** — SMS text-only for v1.
12. **10DLC platform registration fees are itemized to customers as a small,
    separate line item** (not silently absorbed into the per-segment rate),
    even though the underlying registration is platform-level, not per-org.
13. **A minimal, SMS-triggered abuse-monitoring guardrail is in scope** — no
    automated opt-out/complaint-rate enforcement exists anywhere in the
    platform today (confirmed by direct investigation), and SMS's shared
    platform-level 10DLC campaign means one abusive org can get everyone's
    sending throttled — see "Abuse monitoring" below.

## Pricing

Research (Twilio's live pricing pages + 5 competitor pricing pages, fetched
2026-07-05) found the existing placeholder rate in `hail-website/lib/private-rates.ts`
(`sms_cents_per_segment: 1.0`) is **below Hail's own COGS** — blended US
domestic wholesale cost is ~1.23¢/segment (Twilio base 0.83¢ + blended
carrier pass-through ~0.40¢), meaning every domestic SMS today would lose
money. Competitors (Twilio, Telnyx, Plivo, Vonage, Bandwidth) all retail
close to their own wholesale cost (~0.6–1.3¢/segment all-in) — a thin-margin,
price-transparent market, unlike voice (~83–85% margin) or email (~95%
margin) where COGS is negligible. Matching voice/email's margin philosophy on
SMS would price 6–20x above any competitor's all-in rate and invite easy
comparison-shopping churn; SMS is deliberately priced at a lower, ~50% margin
by design.

**New rates** (replacing the placeholder in `private-rates.ts`):

| Item                 | Rate                                            | Rationale                                                                                                                                                                                                                                                                                             |
| -------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| US domestic          | 2.5¢/segment                                    | ~51% margin over 1.23¢ COGS; ~2-3x the raw CPaaS all-in price, justified by bundling (one API, one bill, no 10DLC paperwork)                                                                                                                                                                          |
| Canada               | 3.5¢/segment                                    | Distinct tier — CA wholesale (~1.5–1.7¢ all-in) is higher than US; ~51-57% margin                                                                                                                                                                                                                     |
| Rest of world (flat) | 20¢/segment                                     | Sized to stay profitable even on the priciest researched corridor (Germany, 11.2¢ wholesale, 44% margin); comfortable margin on UK/Australia/India-international. India domestic (DLT) excluded — routed as international/non-DLT instead, pending real DLT cost data                                 |
| Dedicated number     | $2.50/month                                     | ~54% margin over Twilio's ~$1.15/month COGS                                                                                                                                                                                                                                                           |
| 10DLC compliance fee | $1.00/month per org (flat, itemized separately) | Platform registration is shared across all orgs (~$44-60 one-time + $1.50-10/mo total, not per-org), so this comfortably covers the amortized cost with margin to spare; itemized as its own line item rather than folded into the number fee, matching how Twilio/Vonage/Bandwidth itemize 10DLC too |

**Billing mechanism change**: `usage-rater.ts` today only rates per-unit
usage events (segments/minutes/messages). The dedicated-number and 10DLC
fees are recurring monthly charges, not usage events, so this introduces a
new "subscription line item" pass — a cron-driven job that debits
`account_credits` once per org per billing cycle for as long as SMS/a
dedicated number is enabled. `usage_events.channel = 'sms'` is already valid
in the schema; per-segment sends still go through the existing
`write_usage_event`/`has_funds` path unchanged. `lib/billing-tiers.ts`, the
public pricing page, and `price-drift.test.ts`/`private-rates.test.ts` all
get updated to the new figures.

## Architecture & data model (`hail/core/`)

- **Provider layer** (`core/hailhq/core/providers/sms/`): `base.py` defines
  `SmsProvider` (ABC) — `send_sms(to, from_messaging_service_sid, body)`,
  `ensure_messaging_service(org)` (idempotent create/fetch), `attach_number`,
  `detach_number`. `twilio.py` implements `TwilioSmsProvider`, wrapping the
  sync SDK in `asyncio.to_thread` like the existing voice/email adapters. No
  separate `InboundProvider` abstraction (unlike email's SES/SMTP split) —
  Twilio is the only inbound path, so inbound parsing lives directly in the
  webhook route, verified via Twilio's `X-Twilio-Signature` scheme.
- **Data model** (`core/hailhq/core/models.py` + Alembic migrations):
  - `Sms`: `id, organization_id, direction (outbound|inbound), from_number,
to_number, body, status (queued|sent|delivered|failed|undelivered|received),
provider_message_id, segment_count, error_code, created_at` — mirrors
    `Call`'s shape (single status field, not email's multi-event model).
  - **No new suppression table.** A separate compliance workstream already
    landed a generic `Suppression` table (`organization_id, recipient,
channel, reason, source, created_at`, `CHECK (channel IN ('voice',
'email','all'))`) plus `compliance_gate.py`'s `check_call_allowed`/
    `check_email_allowed`. This spec adds one migration widening that CHECK
    constraint to include `'sms'`, and adds `check_sms_allowed()` alongside
    the existing two gate functions — same suppression-hit logic, `channel
= 'sms'`, `recipient` = the phone number.
  - **Consent**: reuses `enforce_consent()` as-is (it's already
    channel-agnostic — takes `recipient_consent`/`consent_source`/
    `message_type` primitives, no Call/Email-specific types). `SmsCreate`
    gets the same four consent fields (`recipient_consent`,
    `consent_source`, `consent_obtained_at`, `message_type`) that
    `CallCreate`/`EmailCreate` already carry. Since this is the third
    hand-copied instance of that block, extract a shared
    `ConsentAttestationMixin` in `schemas.py` as part of this work rather
    than copy-pasting a third time — the repo's "no abstraction without two
    concrete uses" bar is now satisfied.
  - `usage_events.channel = 'sms'` already valid; outbound sends write one
    row with `units = segment_count`.

## Number provisioning (generic, cross-channel) & Sender ID

- **Number provisioning is a standalone, channel-agnostic capability — not
  an SMS feature.** A dedicated `PhoneNumber` belongs to an org and carries
  a `capabilities` set (`voice`, `sms`, and `mms` later); acquiring a number
  and toggling a capability on it are two separate, generic actions:
  - `POST /numbers` / `GET /numbers` / `GET /numbers/{id}` — search/acquire
    a dedicated number (by area code or "any available") and list an org's
    numbers, independent of any one channel.
  - `PATCH /numbers/{id}/capabilities` — enable/disable a capability
    (`voice`/`sms`/future `mms`) on an existing number. Enabling `sms`
    attaches the number to the org's Twilio Messaging Service (created
    lazily on first use) and starts the SMS monthly fee; disabling it
    detaches and stops the fee.
  - This generalizes what would otherwise be an SMS-only "Enable SMS" flow,
    so voice-only self-serve number acquisition (not currently self-serve —
    see the existing "coming soon" pricing-page copy) and future MMS reuse
    the same surface without rework.
  - Two-way/US/Canada SMS still requires a dedicated number (not the shared
    voice pool) for the reasons in Decision 6; the resolution order is: org
    already has a dedicated number with `sms` enabled → use it; org has a
    dedicated number without `sms` → offer to enable the capability; org has
    no dedicated number → offer to acquire one; org is pool-only for voice
    and doesn't go through this generic flow → blocked with a clear error.
- **Sender ID**: a new `SmsSenderIdentity` concept on the org — an
  alphanumeric string (2–11 chars, GSM-7 alphanumeric only, no
  leading/trailing spaces), defaulting to the platform `"HAIL"` if unset. At
  send time, Hail resolves the destination country, checks whether it's a
  no-pre-registration corridor, and picks: the org's custom ID (if set and
  the corridor allows it) → the platform default `"HAIL"` (if the corridor
  requires registration Hail already has) → the org's dedicated number (for
  US/Canada or any corridor where alphanumeric isn't viable). Sender ID
  remains SMS-specific (voice/MMS have no analogous concept).

## Inbound, webhooks & compliance

- **Inbound route**: one shared endpoint, `POST /internal/twilio-sms`,
  signature-verified via `X-Twilio-Signature`. Org is resolved by looking up
  `PhoneNumber` on the payload's `To` field — no per-org webhook URLs needed,
  since every org has its own dedicated number but there's one Hail endpoint.
- **Message flow**: parse `From`/`To`/`Body`/`MessageSid` → resolve org →
  write an inbound `Sms` row (`direction=inbound`, `status=received`) → fan
  out via the org's `WebhookSubscription` using the existing
  `webhook_fanout.py`/`WebhookWorker` (same delivery infra email already
  uses). No separate forwarding mechanism — the webhook is the only inbound
  path.
- **Opt-out (STOP/HELP)**: Twilio's Advanced Opt-Out (enabled per Messaging
  Service) handles carrier-facing auto-reply/blocking; its inbound payload
  includes an `OptOutType` field (STOP/START/HELP) on the triggering message.
  The same inbound route checks for this field and calls
  `compliance_gate.py`'s existing `add_suppression()`/removal path with
  `channel='sms'` — reusing the generic `Suppression` table (see
  "Architecture & data model"), not a new SMS-specific one. No separate
  opt-out webhook needed.
  - New API: `GET /sms/suppressions` (list), `DELETE /sms/suppressions/{number}`
    (manual admin correction only — genuine re-subscription requires the
    recipient to text START). These are SMS-scoped views over the shared
    table (`WHERE channel = 'sms'`), matching how the endpoint is scoped
    even though the storage isn't.
  - `POST /sms` calls the new `check_sms_allowed()` (alongside
    `enforce_consent()`) before sending and rejects with a clear error if
    the destination has opted out — app-level enforcement, not just
    reliance on Twilio silently dropping the message. Same pattern
    `check_call_allowed`/`check_email_allowed` already use for their routes.

## Abuse monitoring

Confirmed by direct investigation: **no automated abuse-signal enforcement
exists anywhere in the platform today.** `compliance_gate.py`'s only runtime
control is a flat, static per-org velocity cap (calls/emails per hour/day) —
it doesn't look at complaint rate, opt-out rate, or any quality signal, so a
steadily-abusive account under the cap sails through untouched. The AUP
(`hail-website/content/legal/aup.md` §7) describes
Warning→Suspension→Termination as available actions "in Hail's discretion,"
but no code implements it.

This matters more for SMS than for voice/email: since **all orgs share one
Hail-owned 10DLC Brand + Campaign** (Decision 2), one org generating a spike
of STOP replies or spam complaints risks Twilio/carrier-side throttling or
suspension of the _entire platform's_ sending — collateral damage to every
other org, not just the offender.

**Scope for this spec**: a minimal, SMS-triggered guardrail, built with the
same channel-agnostic pattern as `Suppression` so it extends to voice/email
later rather than becoming another SMS-only silo:

- New `ChannelSuspension` table (`organization_id, channel, reason,
suspended_at`, `CHECK (channel IN ('sms','voice','email'))`) —
  intentionally separate from `OrgClosure` (that's whole-account closure;
  this is a targeted per-channel pause).
- `check_sms_allowed()` (in `compliance_gate.py`, alongside the suppression
  check) also rejects sends if the org has an active `sms` row in
  `ChannelSuspension`.
- A new scheduled check (piggybacking on the existing worker-lifespan
  pattern in `main.py`) computes each org's rolling 24h opt-out rate
  (`SMS STOP suppressions added / SMS segments sent` in that window) and
  auto-inserts a `ChannelSuspension` row when it crosses a threshold (exact
  threshold TBD during implementation — start conservative, e.g. an
  absolute-count floor plus a percentage, to avoid false-positives on
  low-volume orgs).
- Surfaced to the org in the console (a clear "SMS sending paused" banner,
  not a silent failure) and logged as an audit event; manual ops override to
  lift a suspension, mirroring how `add_suppression`/removal already work.
- **Explicitly out of scope for this spec**: generalizing this to voice/email
  (their abuse profile and volume patterns differ enough to warrant their
  own threshold tuning), and any bounce/complaint-rate signal beyond opt-outs
  (email already surfaces bounce/complaint stats for display —
  `GET /emails/stats` — but nothing consumes them for enforcement; wiring
  that up is a natural fast-follow once the SMS pattern proves out).

## API / CLI / SDK / MCP / OpenAPI surface

- **API**: `hail/api/hailhq/api/routes/sms.py` — `POST /sms`, `GET /sms`,
  `GET /sms/{id}` (gated by `has_funds` + `check_sms_allowed()`,
  `Call`-shaped — single status field, not email's event sub-resource);
  `GET /sms/sender-id` / `PATCH /sms/sender-id`; `GET /sms/suppressions` /
  `DELETE /sms/suppressions/{number}`. A new, separate
  `hail/api/hailhq/api/routes/numbers.py` — `POST /numbers`, `GET /numbers`,
  `GET /numbers/{id}`, `PATCH /numbers/{id}/capabilities` (the generic
  cross-channel number-provisioning flow from the section above; not
  SMS-scoped).
- **CLI** (`cli/internal/cmd/`): `sms*.go` — `hail sms send|list|get`,
  `hail sms sender-id get|set`, `hail sms suppressions list|delete`.
  `numbers*.go` (new, generic) — `hail numbers acquire|list|get`,
  `hail numbers enable-capability|disable-capability`. Cross-channel
  `hail tail` picks up SMS automatically once `/events` resource-id parsing
  accepts `sms:<uuid>` — no new CLI command needed there.
- **SDK** (`hail/sdk/hail/`): `Client.sms.send/list/get`,
  `.sender_id.get/set`, `.suppressions.list/delete`; `Client.numbers.acquire/
list/get/set_capabilities` (generic, new).
- **MCP** (`hail/mcp/hailhq/mcp/tools.py`): `send_sms`, `get_sms`, `list_sms`
  only — voice-shaped (send-and-track), not exposing number/sender-id/
  suppression management, consistent with domain/webhook config also not
  being MCP tools today. `get_events`'s resource-id parsing extended to
  accept `sms:<uuid>`.
- **OpenAPI** (`openapi/openapi.yaml`): new schemas `SmsCreate`,
  `SmsResponse`, `SmsListResponse`, `SmsSenderIdConfig`,
  `SmsSuppressionResponse`/`ListResponse`, `PhoneNumberResponse`/
  `ListResponse`/`CapabilitiesPatch` (generic); new paths for all routes
  above. Regenerated in the same PR per the existing invariant; CLI client
  regenerated from it.

## Console UI (`hail-website/`)

- **`/console/sms`** (new): a paginated activity log (direction, from/to,
  body preview, status, timestamp), mirroring the existing calls/emails
  activity views.
- **Settings additions**: a **Sender ID panel** (custom alphanumeric input,
  defaulting to "HAIL", with inline copy on which corridors it applies to);
  a **Numbers panel** (generic, cross-channel — not SMS-only: lists the
  org's dedicated numbers with a capabilities checklist per number
  (voice/SMS/future MMS) and an "Acquire a number" self-serve CTA, reusable
  from any future channel's settings, not just SMS); a **Suppression list
  panel** (read-only opt-out table, manual-remove gated behind a
  confirmation warning).
- **Pricing page**: replace the "Numbers are pooled today, dedicated numbers
  coming soon" copy with the new tiered SMS pricing and the fact that
  dedicated numbers now ship self-serve.

## Docs, changelog, release notes

- **`docs/setup/sms.md`** (new, following the `setup/twilio.md` template):
  Messaging Service configuration, the one-time platform-level A2P 10DLC
  brand+campaign registration steps (an operator task, not code), required
  env vars, and the Australia/UK/India Sender ID caveats above.
- **`docs/operations.md`**: note that platform 10DLC registration is a
  one-time setup step, and that the new self-serve number flow supersedes
  the old manual `phone_numbers` SQL-seeding pattern for orgs enabling SMS.
- **`CHANGELOG.md`**: move "SMS channel (Twilio outbound and inbound)" out
  of "Deferred to v1.x" into a real dated release section, with SDK/CLI/API
  version bumps alongside it.
- **`README.md`**: check off the existing unchecked `### SMS` milestone boxes
  (Outbound/Inbound — Twilio).
- **Legal docs (`hail-website/content/legal/`)**: `aup.md`, `terms.md`, and
  `dpa.md` already define SMS/text recipients pre-emptively but explicitly
  describe SMS as "planned/future, not current" (per `facts.md`'s own
  instruction). Flip that language to present-tense once SMS ships.
- **`hail/docs/legal/ropa-skeleton.md`**: explicitly notes "SMS is not a live
  channel today... revisit when SMS ships" — add the RoPA entry for SMS as
  part of this launch.
- **`hail/docs/legal/dpia-skeleton.md`**: explicitly names "before SMS
  channel launch" as a review trigger — the DPIA needs a review pass before
  SMS goes live, not just a docs update after the fact (see Risks).

## Testing

- **Backend**: provider adapter tests for `TwilioSmsProvider` (mocked at the
  SDK boundary, matching `test_twilio_voice.py`'s style); `POST /sms`
  send/suppression-block/insufficient-funds/consent-rejection paths; inbound
  webhook signature verification, org resolution by `To`, `OptOutType` →
  generic `Suppression` (`channel='sms'`) read/write; the widened `channel`
  CHECK constraint migration (existing voice/email suppression rows
  unaffected); webhook fanout delivery; the generic number-provisioning flow
  (acquire, capability toggle, existing dedicated number vs. no number vs.
  pool-only-blocked); Sender ID resolution across corridor types
  (custom-allowed / platform-default-only / India-excluded /
  US-Canada-always-number); the abuse-monitoring threshold job
  (opt-out-rate calculation, `ChannelSuspension` insert/lift, blocked-send
  rejection).
- **Website** (vitest): updated `private-rates.test.ts`/`price-drift.test.ts`
  for the new tiers; the recurring subscription-fee rater job; the new
  `/console/sms` page and settings panels.

## Risks & confirm-during-implementation

1. 🛑 **Confirmed (2026-07-07), not just theoretical**: Twilio explicitly
   documents the single-shared-campaign model (this spec's Decision 2) as
   compliance-risky for pooling unrelated businesses — if one org's SMS
   traffic looks abusive/non-compliant to carriers, Twilio may have to
   throttle or suspend the shared registration, taking every other org's
   numbers down with it. The fully-pooled variants of this pattern have been
   outright incompatible with A2P 10DLC since 2023; the semi-pooled variant
   this spec uses still works today but is explicitly flagged, not silently
   tolerated. **Accepted for v1** given the cost/lead-time this avoids (see
   Decision 2) — but this makes the "Abuse monitoring" section's guardrail a
   hard launch dependency, not a parallel nice-to-have: ship SMS without it
   and the platform has no defense against the exact failure mode Twilio
   warns about.
   - Concrete state on Hail's live Twilio account (verified via API,
     2026-07-07): KYB/Trust Hub business profile already approved (SID
     `BU67001392606cddd3e0a83fc874b2d157`); a second, unused duplicate
     profile (`BUfc485de79738063a172680dc07fec64a`, still "in-review")
     should be abandoned before building on the approved one; zero A2P Brand
     Registrations and zero Campaigns exist yet; three US numbers
     (`hail-1/2/3`) are provisioned and ready to attach once a Brand +
     Campaign exist. Non-US numbers (Denmark, Sweden, UK) aren't subject to
     A2P 10DLC at all and can ship independently of this timeline.
   - Concrete next steps: verify the approved Trust Hub profile's Business
     Identity type (confirm it's a standard business profile, not
     accidentally something that would block a straightforward Brand
     registration), register one Standard Brand (Opero Labs ApS is a
     foreign/non-US entity, so Sole Proprietor registration doesn't apply),
     register one Campaign under it, then attach `hail-1/2/3` to a
     Messaging Service's Sender Pool. Budget **~2-3 weeks minimum**
     end-to-end (Twilio's stated 10-15 day campaign review dominates) before
     US SMS can go live — this gates the whole US launch date, independent
     of implementation time.
2. Carrier pass-through fees move without notice (T-Mobile raised its fee
   ~50% effective 2026-01-19) — needs periodic price review, not a one-time
   calculation.
3. Canada likely needs a short code or verified toll-free number for
   reliable A2P delivery, not a plain long code — the modeled Canada COGS
   (~1.5-1.7¢) assumes long code; re-cost once the actual number type for
   Canada is finalized.
4. India domestic (DLT) pricing is quote-only/unpublished — excluded from
   the flat international rate for v1; Australia's brand-new ACMA Sender ID
   Register (effective 2026-07-01) has no published fee yet.
5. International dedicated number costs are essentially unresearched (one
   data point: Germany ~$30/month) — needs dedicated research before
   launching international two-way numbers with dedicated pricing.
6. The blended US carrier COGS (~0.40¢) is an approximation based on an
   assumed AT&T/T-Mobile/Verizon/other traffic mix, not an official Twilio
   blended figure — real COGS shifts with Hail's actual recipient-carrier
   mix.
7. 🟡 **Implementation-ordering risk**: this spec was drafted before the
   parallel compliance workstream (`Suppression`, `enforce_consent`,
   `compliance_gate.py`) merged. This revision reconciles against that
   work's current uncommitted state — re-check for drift against whichever
   of the two actually lands first in `main`, since both are moving targets
   until merged.
8. **DPIA review is an explicit pre-launch gate, not a formality**:
   `docs/legal/dpia-skeleton.md` names "before SMS channel launch" as a
   review trigger for real (it discusses telephony/robocall-style recipient
   harm as an open risk). Don't treat the legal-doc-flip in "Docs, changelog,
   release notes" as sufficient — get the actual DPIA review done before
   flipping SMS to live.
9. **Abuse-monitoring thresholds are unvalidated** (see "Abuse monitoring")
   — the opt-out-rate cutoff is a placeholder pending real traffic data;
   expect to tune it post-launch rather than treating the first guess as
   final.

## Future work (explicit TODOs — out of scope here)

- **MMS** (media messages) — deferred; own pricing/storage model.
- **Self-serve custom alphanumeric Sender ID in registration-required
  corridors** (Australia, UK-protected names) — currently falls back to the
  platform default; per-org registration with lead time is a fast-follow.
- **India domestic/DLT alphanumeric support** — currently excluded/routed as
  a plain international send.
- **Per-country international pricing** — currently one flat rest-of-world
  tier; revisit splitting into more tiers once real corridor volume mix data
  exists.
- **International dedicated numbers** — self-serve acquisition and pricing
  for non-US/Canada dedicated numbers.
- **Generalizing abuse monitoring to voice/email** — the `ChannelSuspension`
  table and pattern are channel-agnostic by design, but tuning
  voice/email-specific thresholds (and wiring in email's existing
  bounce/complaint stats as a signal) is deliberately left for a fast-follow
  once the SMS version proves out.
- **Per-org Twilio subaccount + Brand + Campaign registration** (Twilio's
  recommended, fully-isolated "Architecture #1") — the compliant path that
  removes the shared-fate risk accepted in Decision 2. Real future work, not
  hypothetical: Hail would enroll as a Twilio ISV Reseller/Partner, then
  orchestrate a subaccount + Secondary Customer Profile + Brand + Campaign
  per org via Twilio's API, collecting each org's business info during
  onboarding. Revisit this if the shared-campaign risk materializes (a
  carrier action against the platform) or once volume/customer trust
  justifies the added cost and ~2-3 week per-org lead time.
