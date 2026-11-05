# Call duration limits

Set `max_duration_seconds` on the agent or workspace. The voice worker
announces the limit, allows up to 10 seconds for playback, then deletes the
room. A successful handover starts the separate handover limit.

Outbound SIP legs also receive LiveKit's native `max_call_duration` at
creation. This timer includes ringing and cannot be extended through the
public API after a handover. The worker still enforces the phase limits;
the native timer provides a larger backstop independent of that worker
and subsequent room API requests.

| Leg / configuration | Native budget |
| --- | --- |
| Caller without an eligible transfer | Agent limit + 80s ringing + 30s hangup grace |
| Caller with an eligible transfer | Above + handover limit + 25s pickup checks |
| New handover contact | Handover limit + 30s ringing + 25s pickup checks + 30s grace |
| Agent limit disabled (`0`) | Omit the caller limit; a later handover leg still receives its own positive limit |
| Inbound caller | Existing trunk policy; this change does not alter shared inbound trunks |

For a 300-second agent limit, an ordinary outbound call receives a
410-second native backstop. With an eligible 1800-second handover, its
caller leg receives 2235 seconds and the new contact leg receives 1885
seconds. No extra handover time is reserved when `transfer_call` is
excluded, targets are absent, or all targets are already on the call.
Omitting the native field retains LiveKit's existing defaults; it does
not request an unlimited SIP call.

The 25-second pickup budget covers a 15-second silence check and a
10-second settle check. The hangup grace covers goodbye playback and
bounded room teardown. Native expiry can appear as a normal hangup;
LiveKit's disconnect reason does not identify it as Hail's soft cap.

Implementation: [shared budgets](../../core/hailhq/core/call_limits.py),
[outbound setup](../../api/hailhq/api/routes/calls.py), and
[handover dialing](../../voicebot/hailhq/voicebot/agent.py).
References: [LiveKit SIP API](https://docs.livekit.io/reference/telephony/sip-api/),
[native timer lifecycle](https://github.com/livekit/sip/blob/main/pkg/sip/outbound.go),
and [documented hangup behavior](https://docs.livekit.io/telephony/making-calls/outbound-calls/#hang-up).
