# Telnyx alongside Twilio

Service PR #103. Telnyx stays off until `TELNYX_API_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_SIP_USERNAME`, `TELNYX_PUBLIC_KEY` and `LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID` are set ([`.env.example`](../../.env.example)). No paid carrier test has run.

```bash
# 1. Compare live offers for this organization (Twilio + Telnyx)
curl -X POST "$HAIL_API_URL/numbers/quotes" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"local","capabilities":["voice"]}'
# 2. Buy the recommended offer
curl -X POST "$HAIL_API_URL/numbers" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"local","quote_id":"<recommended_quote_id>"}'
```

Schemas: [`openapi/openapi.yaml`](../../openapi/openapi.yaml). Code: [`number_offers.py`](../../core/hailhq/core/number_offers.py), [`number_orders.py`](../../api/hailhq/api/number_orders.py), carrier adapters in [`core/hailhq/core/providers/`](../../core/hailhq/core/providers/).

## Routing

- The carrier of an owned number is authoritative. Calls use that carrier's LiveKit outbound SIP trunk. Telnyx also sends `X-Telnyx-Username`. Numbers never switch carriers.
- SMS does not use SIP. Telnyx sends use Messaging v2. `POST /sms/telnyx` checks the Ed25519 signature over timestamp and raw body (5 minute limit).
- A finalized event for a message Hail never recorded returns 200. It returns 503 (retry) only for our own number within 5 minutes.

## Offers

- Quotes come from live inventory, live prices and live regulatory rules. No country table.
- Ranking: ready first, then least verification effort, then monthly price, then setup price. Twilio wins exact ties.
- Only USD offers with a positive monthly price count. A carrier error marks the comparison incomplete. It is never read as a zero price or an exemption.
- Telnyx needs an approved requirement group with `customer_reference = hail-<org UUID>`. Twilio needs an approved bundle named `hail-<org UUID>`.
- Twilio address-required numbers stay blocked until address binding exists.
- Cheapest rent is not cheapest total usage. See the TODO below.

## Purchase

1. `POST /numbers` takes a `quote_id`. Quotes are per organization and expire in 10 minutes. Unused quotes are deleted 1 hour after expiry.
2. The server rechecks price and readiness at the carrier. This runs without the org lock or a transaction. If the carrier lookup fails, the answer is 503 (not cached, retry with the same `Idempotency-Key`).
3. Under the org lock it reserves setup plus first month in credits. It then commits the pending number and consumes the quote.
4. Only then does it call the carrier. An unclear result is never retried and never moved to another carrier.
5. Success swaps the reservation for the monthly fee (plus setup). A definite failure refunds it once, and `POST /numbers` answers 409 with the reason instead of 201.
6. Telnyx orders are asynchronous. Only the sweeper checks the order, with a persisted 15-second polling interval. `GET /numbers/{id}` is read-only and returns the last saved state.
7. If the carrier has no record after 1 hour, the order is flagged for operator review. Credits remain reserved: absence from a lookup is not proof of failure. A Telnyx order that exists but is still pending after 2 hours (`PENDING_ORDER_TIMEOUT`) is marked failed and the credits are refunded once. If the carrier finishes it later, release the number at the carrier by hand; the log names the order.
8. A failed order does not hold its number. It can be quoted and bought again.

## Deploy

1. Apply migrations `0044` and `0045` before the quote API. The additive `0045` makes `provider_resource_id` nullable and lets failed orders free their number.
2. Deploy the hail-website change first. Renewal billing reads the stored price.
3. Telnyx account: outbound voice profile, credentials, LiveKit trunk to `sip.telnyx.com` with `numbers: ["*"]`, destination format `+E.164`. The SIP password stays in the LiveKit trunk.
4. Verify: a PT voice quote and order, call setup and hangup, SMS send and receipt, signed callbacks, STOP, repeated purchase, exact-balance purchase, failed-order refund, carrier release.

Inbound voice for many organizations is a separate feature.

## TODO: COSTS database

- [ ] Refresh rental, setup, voice and SMS tariffs with currency, date and source.
- [ ] Include registration fees and surcharges. Define usage weighting before claiming cheapest total spend.
- [ ] Decide how a carrier price change against a stored rental is shown to customers.

Sources: [Telnyx number orders](https://developers.telnyx.com/docs/numbers/phone-numbers/number-orders), [requirement groups](https://developers.telnyx.com/docs/numbers/phone-numbers/requirement-groups), [webhook signatures](https://support.telnyx.com/en/articles/4334722-how-to-leverage-webhooks), [Telnyx LiveKit guide](https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide), [Twilio Regulations API](https://www.twilio.com/docs/phone-numbers/regulatory/api/regulations).
