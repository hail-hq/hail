# Telnyx

Second carrier for numbers, calls and SMS, next to [Twilio](./twilio.md).
When both are configured, `POST /numbers/quotes` compares live inventory,
prices and verification requirements from each and ranks the offers; `POST
/numbers` buys one by `quote_id`. A number never changes carrier: calls from a
Telnyx number go out through the Telnyx trunk, SMS through Telnyx messaging.

```bash
# 1. Live offers for this organization (Twilio + Telnyx)
curl -X POST "$HAIL_API_URL/v1/numbers/quotes" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","capabilities":["voice"]}'
# 2. Buy the recommended offer
curl -X POST "$HAIL_API_URL/v1/numbers" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"country_code":"PT","quote_id":"<recommended_quote_id>"}'
```

Or `hail numbers acquire --country PT --provider telnyx`. Schemas:
[`openapi/openapi.yaml`](../../../openapi/openapi.yaml). Code:
[`carrier_routing.py`](../../../core/hailhq/core/carrier_routing.py),
[`number_offers.py`](../../../core/hailhq/core/number_offers.py),
[`number_orders.py`](../../../api/hailhq/api/number_orders.py).

Telnyx stays off until all five values below are set. With only
`TELNYX_API_KEY` set, quotes list Twilio offers only.

| `.env`                                 | Where it comes from                                                                                                                 |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `TELNYX_API_KEY`                       | Mission Control → **Account → Keys & Credentials → API Keys**                                                                       |
| `TELNYX_CONNECTION_ID`                 | The SIP connection from step 2 (numbers Hail buys are attached to it)                                                               |
| `TELNYX_SIP_USERNAME`                  | The SIP connection's username (sent as `X-Telnyx-Username` on every INVITE)                                                         |
| `LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID` | The LiveKit trunk from step 3                                                                                                       |
| `TELNYX_PUBLIC_KEY`                    | `GET https://api.telnyx.com/v2/public_key` → `data.public` (also under **Keys & Credentials → Public Key**). Verifies SMS webhooks. |

## 1. Outbound voice profile

Every SIP connection needs one. Set the countries you will call; the default
allows only US and CA.

```bash
curl -X POST https://api.telnyx.com/v2/outbound_voice_profiles \
  -H "Authorization: Bearer $TELNYX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"hail","traffic_type":"conversational","service_plan":"global",
       "whitelisted_destinations":["US","CA","GB","PT","SE"],
       "daily_spend_limit":"50","daily_spend_limit_enabled":true}'
```

## 2. SIP connection

An FQDN connection with credentials, as in
[LiveKit's Telnyx guide](https://docs.livekit.io/sip/quickstarts/configuring-telnyx-trunk/).
Pick a username (4–32 letters and digits) and a password (8–128 characters).

```bash
curl -X POST https://api.telnyx.com/v2/fqdn_connections \
  -H "Authorization: Bearer $TELNYX_API_KEY" -H 'Content-Type: application/json' \
  -d '{"active":true,"anchorsite_override":"Latency","connection_name":"hail",
       "user_name":"<username>","password":"<password>","transport_protocol":"TCP",
       "outbound":{"outbound_voice_profile_id":"<profile id from step 1>"}}'
```

The response `data.id` is `TELNYX_CONNECTION_ID`; the username is
`TELNYX_SIP_USERNAME`. The password goes only into the LiveKit trunk below.

## 3. LiveKit outbound trunk

```bash
cat > telnyx-trunk.json <<'EOF'
{"trunk": {"name": "telnyx", "address": "sip.telnyx.com", "numbers": ["*"],
  "auth_username": "<username>", "auth_password": "<password>"}}
EOF
lk sip outbound create telnyx-trunk.json
```

Copy the trunk ID (`ST_…`) into `.env` as `LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID`
and restart `api`. A call from a `telnyx` number with this value empty fails
with `end_reason = carrier_route_failed` before any LiveKit room exists.

## 4. SMS

Nothing to create by hand. `POST /numbers/{id}/enable-sms` creates one Telnyx
messaging profile per organization, with its webhook at
`<HAIL_API_URL>/sms/telnyx`, and attaches the number to it. Delivery status
and inbound messages (including `STOP`/`START`/`HELP`) arrive on that route;
Hail checks the Ed25519 signature with `TELNYX_PUBLIC_KEY` and rejects
requests older than five minutes. `HAIL_API_URL` must be the public URL Telnyx
can reach.

## 5. Countries that need verification

Telnyx will not sell some numbers until a requirement group is approved. Hail
looks for an approved group with `customer_reference` = `hail-<organization
UUID>` for the country and number type; without one the offer is
`verification_required` and cannot be bought. Create the group in Mission
Control (**Numbers → Regulatory requirements**) with that reference, or through
`POST /v2/requirement_groups`.

## Orders

Telnyx orders complete asynchronously. `POST /numbers` reserves setup plus the
first month from credits, places the order and returns the number as
`pending`. A sweeper polls the order every 15 seconds; on success the number
becomes `active` and the reservation turns into the monthly fee. An order still
pending after two hours, or one Telnyx has no record of, is marked `failed` and
refunded once. If Telnyx completes it later, release the number in Mission
Control by hand; the API log names the order.
