# LiveKit Cloud

LiveKit Cloud supplies the media (SIP bridge + WebRTC) in v1. A self-hosted SFU is a later milestone.

## 1. Project + keys

1. Sign up at [cloud.livekit.io](https://cloud.livekit.io).
2. Create a project.
3. From **Settings → Keys**, copy these values into `.env`:
   - `LIVEKIT_URL` — `wss://<project>-<region>.livekit.cloud`
   - `LIVEKIT_API_KEY`
   - `LIVEKIT_API_SECRET`

## 2. SIP inbound trunk

1. **SIP → Inbound Trunks → Create**.
2. Allow calls from your Twilio SIP trunk. Use the IP/user auth that matches your Twilio configuration.
3. Add a **Dispatch Rule** that routes incoming calls to a per-call `individual` room.

## 3. Voicebot worker

Run `docker compose up voicebot`. At startup, the worker registers with LiveKit as a dispatchable agent. The Hail API dispatches it into a room for each call.

For the full flow, refer to [Architecture](../architecture.md).
