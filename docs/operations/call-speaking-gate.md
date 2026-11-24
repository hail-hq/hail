# Call speaking gate

The agent does not listen while its own voice is on the line.

```sql
-- Calls where the worker measured echo, newest first.
SELECT call_id, occurred_at, payload
FROM call_events
WHERE kind = 'audio_echo_detected'
ORDER BY occurred_at DESC
LIMIT 20;
```

## What it does

| Moment                                                              | The agent's input                                                                            |
| ------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Agent audio is playing                                              | Silent. Nothing the other party says is heard.                                               |
| Agent stopped, no echo measured                                     | Live at once. A quick "yes" is heard.                                                        |
| Agent stopped, echo measured on this call                           | Silent for the measured delay plus 0.3 s, then live.                                         |
| Agent is thinking                                                   | Live.                                                                                        |
| Outbound call, until the other party takes a turn after the opening | Live. A menu or voicemail greeting that passed as a person stays heard.                      |
| Outbound call, phone menu being followed                            | Live all the time.                                                                           |
| The goodbye before a hang-up                                        | Live, so someone who talks over it keeps the call open. Silent on a call with measured echo. |
| Someone already talking when a reply starts                         | Live for up to 1.5 s, so they are not cut mid-word.                                          |
| A transferred call is connected                                     | Live all the time (the agent is muted).                                                      |

Code: [`speaking_gate.py`](../../voicebot/hailhq/voicebot/speaking_gate.py),
wired in `entrypoint` in
[`agent.py`](../../voicebot/hailhq/voicebot/agent.py). Browser test calls do
not use it.

## Echo detection

The worker compares the raw caller track with the audio it really played
(waveform, not transcript). Three 0.5 s windows that match at the same delay
confirm echo. The delay is updated when measured again, and dropped after
about 12 s of agent speech that comes back without it. Each confirmed call writes one `audio_echo_detected` call
event: `delay_ms`, `correlation`, `matched_windows`.

Checked on recordings before release: one call with echo (delay about 1.0 s)
was confirmed 3.8 s after the agent first spoke; nine calls without echo, six
of them with a radio playing, gave no confirmation.

## Known limits

- The first agent sentence on an echo call can still return before the echo is
  confirmed.
- The other party cannot stop a reply mid-sentence. So a reply is spoken about
  two sentences at a time
  ([`reply_limit.py`](../../voicebot/hailhq/voicebot/reply_limit.py)). After
  each part the agent is silent for about 1 s. When the other party speaks in
  that pause, the rest is dropped and their turn is answered. When they stay
  quiet, the next part is spoken.
- A reply that calls a tool is spoken whole, and so is every reply while
  LiveKit's interruption model is working.
- Background talk that is going on at the moment a reply starts is heard for
  up to 1.5 s and can still cut that reply.
- Detection runs on the worker's event loop, a few 0.5 s windows per half
  second.

## Phone test before a release that changes it

1. Clean line: say "okay" while the agent talks. It keeps talking.
2. Clean line: answer "yes" within half a second after the agent stops. It
   hears the "yes".
3. Speakerphone at high volume: the agent never answers itself, and the call
   has an `audio_echo_detected` event.
4. Call a number with a phone menu: the agent still follows the menu.

## The LiveKit model instead

`HAIL_ADAPTIVE_INTERRUPTION=true` on the worker turns on LiveKit's
interruption model. The gate then closes only on calls with measured echo. See
[livekit-cloud.md](../public/self-host/livekit-cloud.md).
