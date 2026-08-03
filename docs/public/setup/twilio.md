# Twilio

You need a Twilio account, a phone number, and a SIP trunk that bridges to LiveKit Cloud.

## 1. Account credentials

From [console.twilio.com](https://console.twilio.com), copy these values:

- `TWILIO_ACCOUNT_SID` — starts with `AC…`
- `TWILIO_AUTH_TOKEN` — click "Show" to see the value

Put them in `.env`.

## 2. Phone number

Go to **Phone Numbers → Buy a number**. Select a number with the Voice capability. For outbound SMS, select a number that also has the SMS capability. The carrier fixes these capabilities when you buy the number; you cannot add SMS to a voice-only number later. Note the E.164 format (`+1…`).

## 3. SIP trunk

1. **Elastic SIP Trunking → Trunks → Create new Trunk**.
2. **Origination**: add the URI from [LiveKit Cloud setup](./livekit-cloud.md).
3. **Numbers**: attach the phone number from step 2.
4. Put the Termination URI (for example, `your-trunk.pstn.twilio.com`) in `.env` as `TWILIO_SIP_TRUNK_DOMAIN`.

## 4. Outbound SMS

Hail sends SMS from a dedicated number that you enable for messaging.

1. **Acquire an SMS-capable number** (step 2 above), or pick one you already
   hold. Only numbers with the SMS capability can send.
2. **Enable SMS on the number.** Call `POST /numbers/{id}/enable-sms`. Hail
   attaches the number to your organization's Twilio Messaging Service and
   creates that service the first time. There is one Messaging Service per
   organization; every enabled number joins the same shared sender pool. The
   call is idempotent — an already-enabled number returns its current state.
3. **Send.** Call `POST /sms` with the recipient and body. Hail sends from your
   organization's dedicated number.

**A2P 10DLC (United States).** US carriers deliver application-to-person SMS on
long-code numbers only after you register an A2P 10DLC brand and campaign. Do
this in the Twilio console (**Messaging → Regulatory Compliance → A2P 10DLC**)
before you send to US numbers. Registration is a Twilio-side requirement; Hail
does not manage it.

**Sender ID (optional, rest-of-world).** For destinations that allow it, set a
custom alphanumeric sender with `PATCH /sms/sender-id` (read the current value
with `GET /sms/sender-id`). Clear it by sending `null`. When you set none, Hail
falls back to the platform default sender. The United States and Canada do not
allow alphanumeric sender IDs — messages there always send from the dedicated
number, regardless of this setting.

**Rate limits.** Per-organization send velocity is capped by
`HAIL_VELOCITY_SMS_PER_HOUR` (default 100) and `HAIL_VELOCITY_SMS_PER_DAY`
(default 1000). An abuse monitor suspends an organization's SMS channel when its
opt-out rate is too high — see [operations](../operations.md) for the
`HAIL_SMS_ABUSE_*` variables and how to lift a suspension.

## 5. Inbound SMS & opt-out

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
