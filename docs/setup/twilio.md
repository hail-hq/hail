# Twilio setup

You need a Twilio account, a phone number, and a SIP trunk bridged to LiveKit Cloud.

## 1. Account credentials

From <https://console.twilio.com>:

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
