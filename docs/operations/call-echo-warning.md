# Echo warning fallback

`HAIL_ECHO_WARNING_ENABLED=true` (default: false) enables a single warning when repeated
waveform matches show the agent's played voice returning to the caller track
and the agent's own input reacted to it.
The agent says one fixed line in the call's configured language (English when it
is unknown): “I'm hearing an echo. It's disrupting the call.” The 39 translations
are in [`spoken_lines.py`](../../voicebot/hailhq/voicebot/spoken_lines.py). No
model call is involved. With the flag false, no observer runs.

[Detector and warning](../../voicebot/hailhq/voicebot/echo_warning.py) use a raw,
bounded local reader of the already-subscribed caller track, independently of
voice isolation. The reference contains only audio confirmed by LiveKit's
`playback_progressed` event. Queued or interrupted-away audio is not evidence.
The observer does not change either audio track or playback timing.

Raw echo alone does not trigger the warning, because the input filter may
remove it. The agent's own input must also have reacted twice within 20 s:
speech detected while the agent itself was playing, or a false interruption
reported by the SDK. A quick caller reply after the agent stops does not count.
The warning never starts while the caller is speaking.

Detection requires three separate 500 ms waveform matches within four seconds,
with correlation at least 0.2 and consistent return delay (within 25 ms),
searching 80 ms to 1.6 s. Silence and dominant single-frequency tones are excluded; the matching delay
peak must also exceed other peaks outside a 20 ms neighborhood.
These thresholds were checked on the supplied real recording and synthetic
controls; they do not establish a general false-positive rate. Codec distortion,
room reverberation, long utterances, network jitter, or very weak echo can delay
or prevent detection. Playout confirmation may arrive only after an utterance
ends. This is an echo fallback, not general reverberation detection.

Raw frames are dropped while the agent input is muted for handover.
The warning waits while the agent speaks/thinks and is blocked during opening,
IVR navigation, and connected handover. Detections expire after eight seconds
instead of producing stale warnings after those phases. The line is spoken
with interruptions disabled, so returned audio cannot cut it off.

Once the warning has been spoken, the agent's own input is replaced with
silence while it speaks and for the measured return delay plus 300 ms after
([`EchoGate`](../../voicebot/hailhq/voicebot/echo_warning.py)). The agent then
cannot interrupt or answer itself, whatever model runs the call. Two costs on
these calls: the caller cannot cut the agent off mid-sentence, and a reply that
starts within the return delay after the agent stops (about 1 s on the measured
call) is not heard. A warning that produced no audio leaves the call ungated.

The detector keeps running. It re-measures the delay, lifts the gate after 12
compared agent windows (about 6 s of agent speech) with no echo, and restores
it without a second warning if the echo returns. Room tracks and recordings
are unchanged. This is half-duplex gating, not echo cancellation.

The worker keeps at most 20 s of incoming samples and the latest 12 s of
generated reference per segment in memory. Buffers and native streams close on shutdown.
No audio is saved; `audio_echo_warning` records only delay, correlation, and
matched-window count when the warning is requested. It is not a delivery receipt.

## Phone smoke test before release

Deploy the task branch to a non-production worker with the flag enabled and use
a consenting test caller. A Vercel website preview does not run this worker.

1. With a headset and a quiet line, converse normally: no echo warning or missing
   caller turns.
2. With repeatable speakerphone echo, let the agent finish a few sentences. Expect
   one short warning in the call's language and one `audio_echo_warning` event.
   Verify the line is audible and no extra explanation or stage direction follows.
3. After the warning, wait for the agent to finish, then ask a new question and
   request that the call end. Verify the agent answers, never replies to its own
   voice, and never repeats the warning. Speaking over the agent is ignored.
4. Check opening, IVR, and handover flows: no warning in blocked phases and no
   warnings from expired evidence. End the call and inspect worker logs for
   stream/task errors.

Keep recordings and event timestamps together. A local loopback with real
STT/LLM/TTS checks the pipeline, but does not validate the SIP/PSTN path,
physical speaker/microphone acoustics, or LiveKit Cloud filter licensing.
