# Twilio

You need a Twilio account, a phone number, and a SIP trunk that bridges to LiveKit Cloud.

## 1. Account credentials

From [console.twilio.com](https://console.twilio.com), copy these values:

- `TWILIO_ACCOUNT_SID` — starts with `AC…`
- `TWILIO_AUTH_TOKEN` — click "Show" to see the value

Put them in `.env`.

## 2. Phone number

Go to **Phone Numbers → Buy a number**. Select a number with the Voice capability. Note the E.164 format (`+1…`).

## 3. SIP trunk

1. **Elastic SIP Trunking → Trunks → Create new Trunk**.
2. **Origination**: add the URI from [LiveKit Cloud setup](./livekit-cloud.md).
3. **Numbers**: attach the phone number from step 2.
4. Put the Termination URI (for example, `your-trunk.pstn.twilio.com`) in `.env` as `TWILIO_SIP_TRUNK_DOMAIN`.

## 4. Inbound SMS & opt-out

Point the number's **A Message Comes In** webhook at
`https://<your-api-host>/sms/inbound` (HTTP POST). Hail verifies Twilio's
`X-Twilio-Signature` against `HAIL_API_URL`. Make sure that this value matches
the public URL that Twilio posts to.

**Recognized keywords** (Hail matches them on the message body, case-insensitive):

- Opt out (STOP): `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`
- Opt in (START): `START`, `YES`, `UNSTOP`
- Help: `HELP`, `INFO`

Hail records opt-outs in its own suppression list, regardless of the Twilio
configuration. Hail checks this list before every send.

**Opt-out replies:** By default, **Twilio** replies automatically to
STOP/HELP/START and carrier-blocks opted-out numbers. In that setup, keep
`HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=false`. If you want **Hail** to own the
replies (for example, a non-Twilio provider, or a custom keyword experience),
do the two steps that follow. **Caution: if you disable Twilio's default
opt-out handling, the change is account-wide and requires a Twilio Support
request; there is no API for it.** First, disable Twilio's default opt-out
handling. Then set `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=true`.
