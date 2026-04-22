# LiveKit Cloud setup

LiveKit Cloud handles media (SIP bridge + WebRTC) in v1. Self-hosted SFU is a later milestone.

## 1. Project + keys

1. Sign up at <https://cloud.livekit.io>.
2. Create a project.
3. From **Settings → Keys**, copy into `.env`:
   - `LIVEKIT_URL` — `wss://<project>-<region>.livekit.cloud`
   - `LIVEKIT_API_KEY`
   - `LIVEKIT_API_SECRET`

## 2. SIP inbound trunk

1. **SIP → Inbound Trunks → Create**.
2. Allow calls from your Twilio SIP trunk (IP/user auth per your Twilio config).
3. Add a **Dispatch Rule** routing incoming calls to a per-call `individual` room.

## 3. Voicebot worker

`docker compose up voicebot` — on startup, registers with LiveKit as a dispatchable agent. The Hail API dispatches it into a room per call.

Full flow: [architecture.md](../architecture.md).
