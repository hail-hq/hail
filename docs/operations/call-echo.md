# Call echo cancellation trial

Set `HAIL_ECHO_CANCELLATION_ENABLED=true` on a test voicebot to enable duplex
WebRTC acoustic echo cancellation. It defaults to false. Deployment and enabling
it in production are separate operator actions. Set it back to false and restart
the worker to return to the existing audio path; existing calls keep their path.

The agent's outgoing audio becomes a reference for removing delayed copies and
reflections from its input **before VAD, STT, and interruption detection**.
[Implementation](../../voicebot/hailhq/voicebot/echo.py) uses the existing LiveKit
native APM, with no new provider, model request, or per-minute fee. TTS is paced
in 10 ms chunks so the reference follows speech being played, rather than speech
still queued for playback. The existing Cloud BVC/BVCTelephony noise and background
voice filtering remains active. Self-hosted calls can also use AEC.

Run `pytest voicebot/tests/test_echo.py` from the repo root in the workspace's
Python environment. Synthetic tests cover delayed echoes, multiple reflections,
overlapping caller speech, pacing, pause, interruption, input placement, playback
events, and passthrough after a native failure. They do not establish speech
intelligibility on a real phone connection. Native AEC adds a small processing
delay and needs time to adapt; it cannot guarantee removal of the first echo.

Before enabling broadly, compare the same handset on speakerphone and headset,
with echo cancellation off/on. Include the opening disclosure, real interruptions,
quiet speech, long pauses, and handover. Check that the caller's words remain
intelligible and that the agent does not restart its greeting. Use recorded test
calls only with recording consent. No customer audio fixtures are committed.

This cleans what the agent hears; it does not clean a room's composite recording
or the audio heard by the caller. It addresses reflections of the **agent's own
voice**, not general room reverberation of the caller's voice. AEC is automatic;
we do not make the agent announce echo based on transcript similarity, which can
mistake a caller's deliberate repetition for echo. A future spoken fallback needs
validated residual-echo detection and should ask for a headset only if cleanup
fails persistently.

Background noise/other voices already have BVC. Separate follow-ups worth
measuring are clipping (cannot restore lost samples), low microphone level,
packet loss/jitter, and caller-room reverberation. They need different handling;
blind gain increases or a second generic denoiser can make speech worse.
