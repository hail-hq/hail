# LiveKit Cloud

LiveKit Cloud supplies the media (SIP bridge + WebRTC) in v1. A self-hosted SFU is a later milestone.

## 1. Project + keys

1. Sign up at [cloud.livekit.io](https://cloud.livekit.io).
2. Create a project.
3. From **Settings → Keys**, copy these values into `.env`:
   - `LIVEKIT_URL` — `wss://<project>-<region>.livekit.cloud`
   - `LIVEKIT_API_KEY`
   - `LIVEKIT_API_SECRET`

## 2. SIP outbound trunk

First create the Twilio trunk and credentials in the [Twilio guide](./twilio.md).
Then, in LiveKit Cloud:

1. Open **Telephony → SIP trunks → Create new trunk**.
2. Select **Outbound** and use the Twilio termination domain
   (`<name>.pstn.twilio.com`) as the address.
3. Add the Twilio number in E.164 format and enter the same username/password
   configured on the Twilio trunk.
4. Create the trunk and copy its ID into `.env` as
   `LIVEKIT_SIP_OUTBOUND_TRUNK_ID`.

Hail passes this ID to LiveKit for every outbound call. It does not read a
Twilio trunk-domain environment variable. See LiveKit's
[outbound trunk reference](https://docs.livekit.io/telephony/making-calls/outbound-trunk/)
for the current UI and JSON forms.

`LIVEKIT_SIP_INBOUND_TRUNK_ID` is reserved for a future inbound-calling
release and can remain empty today.

## 3. Voicebot worker

With the local Compose overlay, run:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d voicebot
```

At startup, the worker registers with LiveKit as a dispatchable agent. The
Hail API dispatches it into a room for each call.

For the full flow, refer to [Architecture](../architecture.md).
