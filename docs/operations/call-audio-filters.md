# LiveKit agent input filters

Set `HAIL_AUDIO_FILTER=krisp_viva` on a test voicebot to try Krisp VIVA. SIP
participants use `krisp.voice_isolation_telephony()`; other participants use
`krisp.voice_isolation()`. A worker restart applies the choice to new calls.
The non-default filters are not in the default image: build the voicebot with
`--build-arg VOICEBOT_EXTRAS=filters` (locally: `uv sync --extra filters`).
Without them, or with an unknown value, the worker logs a warning and uses `bvc`.
Production changes require a separate operator action.

| Setting | LiveKit filter |
|---|---|
| `bvc` (existing default) | `BVCTelephony()` for SIP, `BVC()` otherwise |
| `krisp_viva` | Krisp VIVA telephony / standard voice isolation |
| `voice_focus_s` | ai-coustics `QUAIL_VF_S` |
| `voice_focus_l` | ai-coustics `QUAIL_VF_L` |

[Selector](../../voicebot/hailhq/voicebot/audio.py),
[dependencies](../../voicebot/pyproject.toml), and
[LiveKit guidance](https://docs.livekit.io/transport/media/noise-cancellation/)
are the canonical references. These are licensed LiveKit Cloud filters. The
Krisp wrapper is Apache-2.0; its native runtime and ai-coustics use LiveKit's
service terms, as does the existing BVC integration. Self-hosted transport keeps
its existing unfiltered input; this setting does not configure an independent
ai-coustics license.

The filter runs through `AudioInputOptions.noise_cancellation`, before the agent
receives input for VAD/STT. It does not change outgoing playback timing, publish
a second track, or clean composite recordings. Do not stack voice-isolation
models: benchmark one choice at a time. The previous experimental custom AEC
wrapper is removed from the product path.

Validate on representative calls: opening echo, caller interruptions, quiet
speech, pauses, speakerphone and headset. Compare intelligibility and unwanted
transcribed words, not just lower output energy. Voice isolation is not a
promise of echo cancellation. LiveKit's SIP guidance also relies on caller-device
echo cancellation, which Hail cannot toggle on a PSTN handset.

For offline evaluation, use separate send/receive tracks and decoded PCM. Prefer
the least-transformed source WAV, then check the original OGG/Opus recording too.
Listening copies may use identical gain/limiting on every candidate; keep raw
files for metrics. Do not normalize each candidate separately. Keep customer
recordings and transcripts out of Git.

[Echo warning fallback](call-echo-warning.md) detects returning agent audio and
briefly says the echo is disrupting the call when filters fail.
