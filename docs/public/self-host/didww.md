# DIDWW

Second voice carrier, next to [Twilio](./twilio.md). Outbound calls only.
A number's `provider` column picks its trunk
([`core/hailhq/core/carrier_routing.py`](../../../core/hailhq/core/carrier_routing.py)).

```bash
# A call from a DIDWW number goes out through LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID.
curl -X POST "$HAIL_API_URL/calls" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"from":"+351300000000","to":"+14155550100","system_prompt":"Be brief.","recipient_consent":true}'
```

Buying goes through the normal quote flow once `DIDWW_API_KEY` is set. Not
supported on DIDWW: SMS (offers are voice only) and inbound calls.

## 1. DIDWW account

1. Sign up at [my.didww.com](https://my.didww.com).
2. Outbound trunks are off by default. Ask `support@didww.com` to enable
   "Outbound Trunks" on the account.
3. Buy the number: **Buy DIDs**. Some countries hold the number in
   `awaiting_registration` until DIDWW approves the end-user identity and
   address ([end-user verification](https://doc.didww.com/phone-numbers/end-user-verification/index.html)).

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
   validates them (`address_requirement_validations`). A superadmin approves.
2. `POST /numbers` reserves setup + first month, orders the DID
   (`provisioning_state: pending`).
3. The reconciler files the registration (`address_verifications`) once the
   DID exists and polls it. DIDWW approves in 1–3 days → `active`.
4. Rejected → `failed`, the DID is terminated, the monthly fee is refunded,
   the setup fee stays. A pending order is failed and refunded after 7 days.

Schemas: [`openapi/openapi.yaml`](../../../openapi/openapi.yaml). Code:
[`providers/voice/didww.py`](../../../core/hailhq/core/providers/voice/didww.py),
[`providers/verification/didww.py`](../../../core/hailhq/core/providers/verification/didww.py),
[`number_orders.py`](../../../api/hailhq/api/number_orders.py).

A call from a `didww` number with `LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID` empty
fails with `end_reason = carrier_route_failed` before any LiveKit room exists.
The same applies to Twilio numbers when `LIVEKIT_TWILIO_SIP_OUTBOUND_TRUNK_ID` is empty.
