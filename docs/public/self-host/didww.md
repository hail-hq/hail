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

Not supported on DIDWW yet: buying numbers through `POST /numbers`, SMS, inbound
calls. DIDWW sells no SMS on many countries (Portugal national numbers: none).
Check the number's feature list before you buy.

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
- Numbers: the DIDWW number in E.164 (`+351…`)
- Username and password: from step 2

Or with the CLI:

```bash
cat > didww-trunk.json <<'EOF'
{"trunk": {"name": "didww", "address": "fra.eu.out.didww.com",
  "numbers": ["+351300000000"], "auth_username": "<username>", "auth_password": "<password>"}}
EOF
lk sip outbound create didww-trunk.json
```

Copy the trunk ID (`ST_…`) into `.env` as `LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID`
and restart `api`.

## 4. Register the number in Hail

`POST /numbers` cannot buy from DIDWW yet. Insert the row by hand, same as a
[pool number](./operations.md#phone-number-pool):

```sql
INSERT INTO phone_numbers
  (organization_id, e164, country_code, number_type, capabilities,
   provider, provider_resource_id, provisioning_state, acquired_at)
VALUES
  ('<org uuid>', '+351300000000', 'PT', 'national', ARRAY['voice'],
   'didww', '<DIDWW DID id>', 'active', now());
```

`provider_resource_id` is the DID's `id` from `GET /v3/dids` at DIDWW
([API](https://doc.didww.com/api3/2026-04-16/index.html)). `capabilities`
must not include `sms` unless the DID lists SMS.

A call from a `didww` number with `LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID` empty
fails with `end_reason = room_create_failed` before any LiveKit room exists.
