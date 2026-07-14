# Twilio

You need a Twilio account, a phone number, and a SIP trunk bridged to LiveKit Cloud.

## 1. Account credentials

From [console.twilio.com](https://console.twilio.com):

- `TWILIO_ACCOUNT_SID` — starts with `AC…`
- `TWILIO_AUTH_TOKEN` — click "Show" to reveal

Put them in `.env`.

## 2. Phone number

**Phone Numbers → Buy a number** → pick a number with Voice capability. Note the E.164 format (`+1…`).

## 3. SIP trunk

1. **Elastic SIP Trunking → Trunks → Create new Trunk**.
2. **Origination**: add the URI from [LiveKit Cloud setup](./livekit-cloud.md).
3. **Numbers**: attach the phone number from step 2.
4. Put the Termination URI (e.g. `your-trunk.pstn.twilio.com`) in `.env` as `TWILIO_SIP_TRUNK_DOMAIN`.

## 4. Inbound SMS & opt-out

Point the number's **A Message Comes In** webhook at
`https://<your-api-host>/sms/inbound` (HTTP POST). Hail verifies Twilio's
`X-Twilio-Signature` against `HAIL_API_URL`, so that value must match the public
URL Twilio posts to.

**Recognized keywords** (matched on the message body, case-insensitive):

- Opt out (STOP): `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`
- Opt in (START): `START`, `YES`, `UNSTOP`
- Help: `HELP`, `INFO`

Hail records opt-outs in its own suppression list (checked before every send)
regardless of Twilio configuration.

**Opt-out replies:** By default **Twilio** auto-replies to STOP/HELP/START and
carrier-blocks opted-out numbers. Leave `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false`
in that setup. To have **Hail** own the replies (e.g. a non-Twilio provider, or a
custom keyword experience), first disable Twilio's default opt-out handling — this
is **account-wide and requires a Twilio Support request; there is no API for it** —
then set `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=true`.
