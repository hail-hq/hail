# Echo warning fallback

`HAIL_ECHO_WARNING_ENABLED=true` (default) enables a single warning when repeated
waveform matches show the agent's played voice returning to the caller track.
The reply says, in the call's language, that its own voice is coming back and
asks the caller to disable speakerphone or use headphones. It does not claim
that Hail has fixed the echo. Set the flag false to disable the observer.

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

The worker keeps at most 20 s of incoming samples and 12 s of generated reference
per segment in memory. Buffers and native streams close on warning or shutdown.
No audio is saved; `audio_echo_warning` records only delay, correlation, and
matched-window count when the warning is requested. It is not a delivery receipt.
