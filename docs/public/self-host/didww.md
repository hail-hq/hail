# DIDWW

Second voice carrier, next to [Twilio](./twilio.md). Calls in and out.
A number's `provider` column picks its trunk
([`core/hailhq/core/carrier_routing.py`](../../../core/hailhq/core/carrier_routing.py)).

```bash
# A call from a DIDWW number goes out through LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID.
curl -X POST "$HAIL_API_URL/calls" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"from":"+351300000000","to":"+14155550100","system_prompt":"Be brief.","recipient_consent":true}'
```

Buying goes through the normal quote flow once `DIDWW_API_KEY` and
`LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID` are both set; with either one empty no
DIDWW number is quoted. Not supported on DIDWW: SMS (offers are voice only).
Inbound calls: see [below](#inbound-calls).

## 1. DIDWW account

1. Sign up at [my.didww.com](https://my.didww.com), billed in **USD**. Hail
   reads DIDWW's SKU prices as USD cents with no currency conversion or
   check; a non-USD account will silently mis-price every DIDWW number.
2. Outbound trunks are off by default. Ask `support@didww.com` to enable
   "Outbound Trunks" on the account.
3. Numbers are bought through Hail ([§5](#5-buying-a-number)); the DIDWW
   panel is only needed for the trunk and the API key.

## 2. DIDWW outbound trunk

**Voice → Outbound Trunks → Create** ([guide](https://doc.didww.com/voice/outbound-trunks/how-to-guides/create-outbound-trunk.html)):

- Authentication: **Credentials & IP-Based**.
- Allowed SIP IPs and Allowed RTP IPs: LiveKit Cloud's static ranges
  `143.223.88.0/21`, `161.115.160.0/19`, `153.57.128.0/18`
  ([LiveKit static IPs](https://docs.livekit.io/deploy/admin/regions/endpoints/)).
  If the form rejects a range, ask DIDWW support to add it.
- CLI settings: add the number to the allowed Caller IDs. On mismatch:
  **Reject call**.
- Save. Copy the SIP username and password for step 3.

DIDWW uses SIP digest on INVITE (realm `out.didww.com`), no REGISTER.
Signaling hosts: `fra.eu.out.didww.com` (EU), `nyc.us.out.didww.com` (US),
`any.out.didww.com` (anycast)
([SIP information](https://doc.didww.com/voice/outbound-trunks/outbound-sip-information.html)).

## 3. LiveKit outbound trunk

In LiveKit Cloud, **Telephony → SIP trunks → Create new trunk → Outbound**:

- Address: `fra.eu.out.didww.com`
- Numbers: `*` (any DIDWW number; the Twilio trunk uses the same). Hail picks
  the trunk by the number's `provider`, so no per-number list is needed.
- Username and password: from step 2

Or with the CLI:

```bash
cat > didww-trunk.json <<'EOF'
{"trunk": {"name": "didww", "address": "fra.eu.out.didww.com",
  "numbers": ["*"], "auth_username": "<username>", "auth_password": "<password>"}}
EOF
lk sip outbound create didww-trunk.json
```

Copy the trunk ID (`ST_…`) into `.env` as `LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID`
and restart `api`.

## 4. API key

my.didww.com → **API** → create a key. Put it in `.env`:

```
DIDWW_API_KEY=<key>
DIDWW_ENVIRONMENT=production
```

`DIDWW_ENVIRONMENT=sandbox` points every call at `sandbox-api.didww.com`
(sandbox key from the Sandbox User Panel → API → DIDWW API 3). Restart `api`.

## 5. Buying a number

```bash
curl -X POST "$HAIL_API_URL/numbers/quotes" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"national","capabilities":["voice"]}'
# → offers[].provider == "didww", readiness "ready" or "verification_required"
curl -X POST "$HAIL_API_URL/numbers" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","number_type":"national","quote_id":"<quote_id>"}'
```

Order of events for a country that needs end-user registration:

1. `readiness: verification_required` → the customer fills `/verifications`
   (console wizard). Hail creates the identity, address and proofs at DIDWW and
   validates them (`address_requirement_validations`). Papers that pass are
   approved at once. No person reviews them
   ([`create_verification`](../../../api/hailhq/api/routes/verifications.py)).
2. `POST /numbers` reserves setup + first month, orders the DID
   (`provisioning_state: pending`).
3. The reconciler files the registration (`address_verifications`) once the
   DID exists and polls it every 15 minutes. DIDWW approves in 1–3 days →
   `active`.
4. Rejected → `failed`, the DID is terminated, the monthly fee is refunded,
   the setup fee is charged (DIDWW billed it and does not refund).
5. Still pending after 7 days → `failed`. DID exists: terminated, monthly fee
   refunded, setup fee charged. No DID: setup and monthly fee refunded.

If the terminate call fails, the DID id is kept in
`provisioning_metadata.unreleased_resource_id` and the reconciler retries every
minute until DIDWW accepts it.

Schemas: [`openapi/openapi.yaml`](../../../openapi/openapi.yaml). Code:
[`providers/voice/didww.py`](../../../core/hailhq/core/providers/voice/didww.py),
[`providers/verification/didww.py`](../../../core/hailhq/core/providers/verification/didww.py),
[`number_orders.py`](../../../api/hailhq/api/number_orders.py), the `didww`
entry of `CARRIERS` in
[`carrier_routing.py`](../../../core/hailhq/core/carrier_routing.py).

A call from a `didww` number with `LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID` empty
fails with `end_reason = carrier_route_failed` before any LiveKit room exists.
The same applies to Twilio numbers when `LIVEKIT_TWILIO_SIP_OUTBOUND_TRUNK_ID` is empty.

## Numbers Hail cannot sell

The catalog ([`costs/didww.json`](../../../costs/didww.json)) marks two kinds
of DIDWW number that the console and `POST /numbers/quotes` never offer:

```bash
jq -r '.numbers[] | select(.by_request) | "\(.country_code):\(.number_type)"' costs/didww.json
jq -r '.numbers[] | select(.receive_only) | "\(.country_code):\(.number_type)"' costs/didww.json
```

- `by_request: true` (40 rows on 2026-10-07): DIDWW picks the number when
  the order is placed, so there is no number to show and buy. Examples:
  PT national, DE local, GB local, FR local, ES local. The console shows
  "set up by our team, not bought here" with an **Ask support for a number**
  button that emails hi@hail.so. Twilio or Telnyx sell most of these
  country and type pairs; only these 8 have no other carrier: AL local,
  CH toll-free, DE national, DK national, HK national, IE national,
  MT national, NG local. Fulfilment:
  [`docs/operations/didww-by-request.md`](../../operations/didww-by-request.md).
- `receive_only: true` (86 rows, mostly toll-free): the number takes calls
  but cannot place them (DIDWW feature `voice_in` without `voice_out`).

The weekly catalog sync sets both from DIDWW's number groups
([runbook](../../operations/number-catalog-sync.md)); the API purchase gate
skips marked rows
([`telephony_catalog.py`](../../../core/hailhq/core/telephony_catalog.py)).

## Inbound calls

A DID takes calls through the voice IN trunk it is assigned to. Hail assigns
the DID when it routes calls to an agent; you create the trunk once.

1. **Voice → Inbound Trunks → Create**: type SIP, host
   `<project>.sip.livekit.cloud`, port 5060, transport TCP. Copy the trunk
   id into `.env` as `DIDWW_VOICE_IN_TRUNK_ID`. Set `DIDWW_API_KEY`
   (**my.didww.com → API**).
2. Create the LiveKit inbound trunk and dispatch rule
   ([LiveKit Cloud §4](./livekit-cloud.md#4-inbound-calls)) and set
   `LIVEKIT_SIP_INBOUND_TRUNK_ID`.

`PATCH /v1/numbers/{id}` with `voice_agent_id` then assigns the DID to the
trunk (`PATCH /v3/dids/{id}`, relationship `voice_in_trunk`) and lists the
number on the LiveKit trunk; `null` clears both ([Agents](../agents.md)).
A DID added by hand without a stored DID id is looked up by number.
Reference: DIDWW [voice IN trunks](https://doc.didww.com/voice/inbound-trunks/index.html).
