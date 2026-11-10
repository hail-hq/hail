# Echo warning fallback

`HAIL_ECHO_WARNING_ENABLED=true` (default) enables a single warning when repeated
waveform matches show the agent's played voice returning to the caller track.
The reply says only, in the call's language: “I'm hearing an echo—it's disrupting
the call.” It adds no advice or questions and does not claim the echo is fixed. Set the flag false to disable the observer.

[Detector and warning](../../voicebot/hailhq/voicebot/echo_warning.py) use a raw,
bounded local reader of the already-subscribed caller track, independently of
voice isolation. The reference contains only audio confirmed by LiveKit's
`playback_progressed` event. Queued or interrupted-away audio is not evidence.
The observer does not mute or change either audio track or playback timing.

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
instead of producing stale warnings after those phases. The line is generated
without tools and with interruptions disabled, so returned audio cannot
immediately cut it off. It may overlap caller VAD because echo itself triggers
VAD; there is no blanket caller-muting rule.

After confirmation, private conversation context tells subsequent replies that
returned fragments may be echo and the warning has already been requested.
For fragments without new intent, the model returns a structured silence marker
which the existing speech sanitizer drops. This is model-dependent handling,
not acoustic echo cancellation or a guarantee against self-replies.

The worker keeps at most 20 s of incoming samples and 12 s of generated reference
per segment in memory. Buffers and native streams close on warning or shutdown.
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
3. After the warning, ask a new question, interrupt, and request that the call end.
   Verify real caller speech remains responsive and the warning never repeats.
4. Check opening, IVR, and handover flows: no warning in blocked phases and no
   warnings from expired evidence. End the call and inspect worker logs for
   stream/task errors.

Keep recordings and event timestamps together. A local loopback with real
STT/LLM/TTS checks the pipeline, but does not validate the SIP/PSTN path,
physical speaker/microphone acoustics, or LiveKit Cloud filter licensing.
