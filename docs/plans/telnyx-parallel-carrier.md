# Telnyx alongside Twilio — implementation and research

Reviewed 2026-09-23. Continues service PR #103 with a companion hail-website PR.
This supersedes the discovery-only plan. Telnyx is unavailable until its API,
SIP and webhook configuration is supplied. No paid carrier test has been run.

## Routing implemented

An owned number's carrier is authoritative. New calls store that carrier and use
its LiveKit outbound SIP trunk; Telnyx also sends `X-Telnyx-Username` on the first
INVITE. The existing Twilio trunk remains separate. Hangup and call lifecycle
continue through LiveKit's room/participant flow; a SIP call ID is not a Telnyx
Call Control ID and must not be passed to Call Control's hangup API.

SMS is not sent over SIP. Telnyx sends use Messaging v2 with the owned number as
`from`; enabling SMS creates/reuses an organization-specific messaging profile
and attaches the owned phone-number resource. `/sms/telnyx` verifies the Ed25519
signature over the timestamp and raw body, bounds timestamp skew to five minutes,
and handles inbound messages and finalized delivery receipts. Inbound STOP/START
uses the existing organization suppression mechanism; duplicate messages and
terminal delivery callbacks do not fan out twice. Keep carrier opt-out responses
enabled when Hail's compliance auto-replies are disabled.

Sources: [Telnyx LiveKit guide](https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide),
[LiveKit outbound trunks](https://docs.livekit.io/telephony/making-calls/outbound-trunk/),
[Sending SMS](https://developers.telnyx.com/docs/messaging/messages/send-message),
[Receiving SMS](https://developers.telnyx.com/docs/messaging/messages/receive-message),
[Webhook signatures](https://support.telnyx.com/en/articles/4334722-how-to-leverage-webhooks).

## Country selection and regulatory eligibility

The supplied `twilio-vs-telnyx.md` comparison is a research starting point, not a
routing table. Country coverage does not establish current stock, outbound voice,
two-way SMS, approved end-user documents, or a lower total usage bill. No country
winner list is embedded in the implementation.

`POST /numbers/quotes` queries both configured carriers. It filters actual
inventory by requested capabilities, reads live setup/monthly prices, and checks
live regulatory requirements. Omitting `number_type` compares all supported types.
Only USD offers with valid, positive monthly prices are eligible. Carrier errors
are reported as an incomplete comparison, never as zero prices or exemptions.

Automatic selection ranks ready-to-purchase offers ahead of blocked offers, then
monthly rental, setup cost, and remaining requirement count. The console's Advanced
settings can restrict provider/type. This is the lowest rental among returned,
ready offers—not a claim to optimize all future call/SMS usage. Existing numbers
never switch carriers implicitly.

Telnyx requirement groups must match country/type/ordering action, be approved,
and have `customer_reference = hail-<organization UUID>`. This is an operator-owned
binding: customers cannot supply an arbitrary group ID in a purchase request.
Twilio business-end-user regulations are queried live; an approved bundle must
match the regulation and the organization-specific friendly name. Address-required
Twilio stock remains blocked until an address binding flow is implemented. Empty
Twilio requirement objects mean no documents are required; a regulation resource
alone is not a purchase blocker (verified against US local inventory).

The comparison's general country recommendations cannot replace end-customer
compliance. In particular, using Hail/Opero's company details for unrelated end users
would not establish their eligibility. An approved Telnyx group is rechecked at
purchase; Telnyx's order/activation result remains authoritative for locality rules.
Document collection and individual-end-user verification are not introduced by this
PR. The console shows outstanding requirements and a verification support link.

Sources: [Twilio Regulations API](https://www.twilio.com/docs/phone-numbers/regulatory/api/regulations),
[Twilio account-specific prices](https://www.twilio.com/docs/phone-numbers/pricing),
[Telnyx requirement groups](https://developers.telnyx.com/docs/numbers/phone-numbers/requirement-groups),
[Mandatory requirement groups](https://support.telnyx.com/en/articles/9801714-requirement-groups-for-ordering-phone-numbers),
[Official Telnyx OpenAPI](https://github.com/team-telnyx/openapi/blob/master/openapi/spec3.json).

## Portugal

Search PT for outbound voice first, then voice + SMS. Do not infer SMS support from
the country or a generic “voice” feature. Telnyx documents the `emergency` inventory
feature as its outbound-capable filter; Hail requires it for call offers. This
filter does not enable emergency calling or claim that Hail supports emergency use.
Search filters use national digits when matching a specific number, and returned
E.164 values are checked exactly.

Portugal is among Telnyx's mandatory requirement-group markets. The applicant must
satisfy the current local/national-number rules; approval and live inventory must
be checked on the account. If no combined voice/SMS offer exists, the UI returns
no matching offer and lets the user change capabilities rather than advertising
unsupported SMS. A separate SMS number may be needed.

Twilio's pricing catalogue can list PT without purchasable stock. The local PT
inventory request did not yield a verified offer during this session; this does
not prove permanent unavailability. Telnyx PT inventory and account eligibility
remain unverified because Telnyx credentials have not been available.

Source: [Telnyx inventory constraints and capabilities](https://developers.telnyx.com/docs/numbers/phone-numbers/number-search/).

## Purchase, reconciliation and billing

Quotes are stored server-side, scoped to the organization, and expire after ten
minutes. `POST /numbers` accepts `quote_id` and an optional provider restriction.
The server rechecks the exact number, price and compliance before reserving setup
plus first-month credits under the organization's advisory lock. The quote is
consumed and the pending number committed before calling the carrier. Reusing a
quote cannot purchase twice, even with a different HTTP idempotency key.

Telnyx orders are asynchronous. Persist the order ID; wait for successful order,
met requirements and an active owned phone-number resource. An order-phone ID is
not the owned phone-number ID. Background reconciliation also recovers an ambiguous
POST via its durable customer reference. Never repeat an ambiguous purchase POST
or fail over to another carrier. If the carrier cannot establish the outcome,
credits stay reserved pending reconciliation; operator review may be needed.

Definite failure returns the reservation once. Activation exchanges it atomically
for the first monthly fee and any setup fee. The first month uses the same reference
as the renewal rater. The number stores its provider and USD monthly-price snapshot;
renewal/dunning uses that snapshot and never falls back to Twilio prices for Telnyx.
Pending numbers cannot place calls, send SMS or be released as if already active.
Release/dunning dispatch by stored carrier; failed attempts can be dismissed without
creating a monthly charge. Legacy purchases that omit carrier/quote retain their
existing Twilio contract; new automatic/multi-carrier purchases require a quote.

Source: [Telnyx number orders](https://developers.telnyx.com/docs/numbers/phone-numbers/number-orders).

## Activation and deployment

1. Apply migration 0044 before deploying the quote API. Deploy the companion
   website changes before enabling Telnyx purchases so renewal billing reads the
   price snapshots.
2. Configure `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_SIP_USERNAME`,
   `TELNYX_PUBLIC_KEY`, and `LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID`. All are documented
   in `.env.example`; no secret belongs in the browser or repository.
3. Telnyx account verification, SIP outbound voice profile/destination permissions,
   credentials, and a LiveKit trunk pointed at `sip.telnyx.com` must be established.
   Use `numbers: ["*"]` on the dedicated LiveKit outbound trunk; Hail still
   restricts caller ID to the authenticated organization's active numbers. Set
   Telnyx destination number format to `+E.164`. Scope credentials and
   use TLS/SRTP where supported. The application sends the username header; the
   SIP password stays in LiveKit trunk configuration.
4. This adds Hail's existing outbound-call use case. Multi-tenant inbound voice
   dispatch is a separate feature; do not configure a shared unscoped inbound room.
5. Verify an eligible PT voice quote/order, call setup and teardown, SMS send and
   receipt if the actual number supports SMS, signed callbacks, STOP suppression,
   duplicate purchase requests, exact-balance purchases, failed-order refunds,
   monthly debit deduplication, and carrier release.

## Deferred TODO: Hail COSTS database

- [ ] Refresh provider-specific rental/setup, voice destination and SMS segment
      tariffs with currencies, effective dates, and provenance.
- [ ] Include messaging registration fees/carrier surcharges and verified account
      discounts; define expected-usage weighting before claiming cheapest total
      calls + SMS spend. Current default selection compares number rental/setup.
- [ ] Reconcile subsequent carrier repricing against stored rental snapshots with
      explicit customer-visible handling rather than silently applying Twilio rates.
