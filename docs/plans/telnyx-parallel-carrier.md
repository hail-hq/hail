# Telnyx alongside Twilio

Status: discovery foundation only. Twilio remains the production carrier.

The read-only TelnyxNumberDiscovery adapter returns current inventory with explicit
capabilities, currency, upfront and monthly carrier costs. It does not purchase,
activate, or expose new offers in the console. Missing prices and mismatched
capabilities are not offers. Quotes need revalidation before any eventual order.

## Next implementation steps

- Add Telnyx credentials and per-provider LiveKit SIP trunks. Existing PhoneNumber,
  Call and Message rows already have provider fields; route operations by the stored
  provider, including release, dunning, account deletion, SMS and call inspection.
- Model search, regulatory eligibility, purchase pending and activation separately.
  Telnyx orders can succeed while documentation approval is still pending. Implement
  durable order IDs, reconciliation and verified webhooks before enabling purchases;
  never blindly retry a timed-out purchase through another carrier.
- Add Telnyx requirement-group collection and approval UX. For Portugal, verify the
  applicant's local/national requirements and current stock. Confirm outbound calling
  and two-way SMS on the actual number; inbound voice capability alone is insufficient.
- Configure a Telnyx messaging profile and inbound/outbound SMS processing separately
  from voice. Confirm LiveKit inbound/outbound calling with a provisioned test number.
- Keep credit reservation and final ledger charge idempotent across asynchronous
  activation and failures. Include setup fees as well as monthly fees, with a defined
  refund policy and renewal behavior. Do not use Twilio prices to bill Telnyx numbers.

## Deferred TODO: update Hail COSTS database

- [ ] Refresh COSTS after provider integration: version provider-specific number rental,
      setup, voice destination and SMS segment rates, currencies and effective dates.
- [ ] Choose cheapest eligible offer per country/type/capabilities only after checking
      inventory, regulatory approval and routing readiness. Show rental and usage prices
      separately: the cheapest rental is not necessarily cheapest for calls or SMS.
- [ ] Define how expected usage weights recurring and usage costs before automatically
      selecting a provider. Preserve explicit provider choice and never migrate existing
      numbers implicitly.

## Sources and live verification

- https://developers.telnyx.com/api-reference/phone-number-search/list-available-phone-numbers
- https://developers.telnyx.com/docs/numbers/phone-numbers/number-search/
- https://support.telnyx.com/en/articles/5469551-international-numbers-required-documents
- https://support.telnyx.com/en/articles/9801714-requirement-groups-for-ordering-phone-numbers

Reviewed 2026-09-23. No authenticated Telnyx inventory or provisioning test has been
performed; a Telnyx API key and an eligible account are needed for that verification.
