# Voicebot: answering machine detection + agent DTMF

**Date:** 2026-07-21
**Status:** approved, not yet implemented
**Scope:** `voicebot/`, `core/`, one alembic migration

Two capabilities for outbound voice calls:

1. **AMD** — classify who answered (person, IVR, voicemail, dead mailbox) and
   hang up instead of talking to a machine.
2. **`send_dtmf`** — let the agent press keypad digits at any point in a call.

No API surface change: no new `POST /calls` fields, no `openapi.yaml` regen, no
CLI codegen.

## Background

`voicebot/hailhq/voicebot/agent.py` owns the call lifecycle. Today every call
speaks the mandatory AI disclosure immediately after `session.start()`,
regardless of who — or what — picked up. A voicemail greeting gets a full
conversation attempt.

The API service, not the voicebot, creates the SIP participant
(`api/hailhq/api/routes/calls.py:375`) with identity `caller-{call_id}`. So the
LiveKit docs' pattern of wrapping `create_sip_participant` in the AMD context
manager does not apply here; AMD attaches to the already-present participant by
identity instead.

## Dependency bump

The lock pins `livekit-agents==1.5.6`; latest is `1.6.6`. `pyproject.toml`
already declares `>=1.5,<2`, so this is `uv lock --upgrade` for
`livekit-agents` and the eight `livekit-plugins-*` packages — no manifest edit.

Two upstream fixes land directly on these features:

- **1.5.17** — `fix(amd): start listening on lost publisher, forward realtime
transcripts, harden attribute wait` (#5918).
- **1.6.5** — `fix: send DTMF to the active session room` (#6360).

  1.6.6 also adds the `participant_identity`, `detection_options`, `stt`, and
  `suppress_compatibility_warning` parameters that 1.5.6 lacks.

The bump ships in the same commits as the features. Two behavior changes in the
range touch existing voicebot code and must be re-verified against the current
test suite:

- #6284 "set default shutdown reason" — the `session.on("close")` →
  `CallEndReason` mapping at `agent.py:780` reads `ev.reason`.
- `silero.VAD.load()` switched to ONNX — `prewarm` must still load.

## AMD

### New module: `voicebot/hailhq/voicebot/amd.py`

`agent.py` is 866 lines; AMD gets its own module rather than growing it.

Exports:

- `MACHINE_HANGUP_CATEGORIES: frozenset[str]` — `{"machine-vm", "machine-unavailable"}`.
- `amd_end_reason(category: str) -> str` — maps those two to `CallEndReason` values.
- `run_amd(session, call_id) -> AMDPredictionEvent | None`.

`run_amd` constructs the detector as:

```python
AMD(
    session,
    llm=session.llm,
    stt=session.stt,
    participant_identity=f"caller-{call_id}",
    detection_options={"no_speech_threshold": 6.0},
    suppress_compatibility_warning=True,
)
```

and runs it as `async with ... as detector: return await detector.execute()`.

Everything is wrapped in `try/except Exception → log + return None`. An AMD
failure must never kill a call — the same posture as `build_tools_safely`.

Rationale for each argument:

- **`llm` / `stt` passed explicitly.** On 1.6.6, leaving them unset makes AMD
  auto-select LiveKit Inference (`google/gemini-3.1-flash-lite` +
  `cartesia/ink-whisper`) whenever `LIVEKIT_URL` is Cloud and the API key and
  secret are set (`amd/detector.py:175-186`) — which is Hail's deployed
  config. That would send the greeting transcript to a vendor we do not
  otherwise use, bill it outside the `usage_events` model, and override the BYO
  provider precedence that `pipeline.py` exists to enforce. Passing the
  session's own layers keeps classification on the brain the org chose.
  `session.llm` is typed `LLM | RealtimeModel | None`; Hail never uses
  `RealtimeModel`, so an `isinstance` narrowing satisfies mypy.
- **`suppress_compatibility_warning=True`.** Follows from the above: the
  session LLM is not on LiveKit's evaluated list, so the warning would fire on
  every call. Hail's house STT is Deepgram, which _is_ on the list.
- **`participant_identity`.** Matches the identity the API sets at
  `routes/calls.py:379`. Without it AMD attaches to the first remote audio
  track, which is non-deterministic if the room ever holds another participant.
- **`no_speech_threshold: 6.0`** (default 10.0). This is the ceiling on how
  long a live-but-silent callee waits before hearing the disclosure. Ten
  seconds of dead air is a worse regression than a slightly less certain
  verdict.
- **`ivr_detection`** left at its default `True`, so a `machine-ivr` verdict
  automatically starts LiveKit's IVR navigation.

### Entrypoint wiring

In `entrypoint`, between `await session.start(...)` and `speak_greeting`. The
soft-cap task and `ctx.add_shutdown_callback(_shutdown)` must both be set up
_before_ this block: the hangup path returns early, and without the callback
registered the Call row would never finalize and the pool number would leak.
Arming the soft cap first also keeps it bounding the call from pickup rather
than from after detection and greeting playout.

```
result = await run_amd(session, call_id)          # own timeout backstop
await write_call_event(call_id, "amd_result", {category, transcript[:500]})
if result and result.category in MACHINE_HANGUP_CATEGORIES:
    captured["status"] = "no_answer"
    captured["end_reason"] = amd_end_reason(result.category)
    await ctx.delete_room()
    ctx.shutdown(reason=captured["end_reason"])
    return
try:
    await speak_greeting(session, metadata)       # session may have died
except Exception:
    log
```

`speak_greeting` is guarded because AMD holds it for the length of the
detection window, and the callee can hang up inside that window —
`AgentSession.say` raises once the activity is torn down.

`run_amd` wraps `detector.execute()` in `asyncio.wait_for`. AMD's own 20s
budget is armed only after the SIP audio track publishes, and the wait for
that publication takes no timeout, so a leg that never publishes would
otherwise block `entrypoint` indefinitely.

The `amd_result` event is written on every call, machine or not — `CallEvent.kind`
is free `Text` (`core/hailhq/core/models.py:150`), so this needs no schema change
and gives per-call observability into classification quality.

Hanging up mirrors `make_agent_hangup`: `delete_room` disconnects the phone leg,
then `ctx.shutdown` releases the job and drives `_shutdown` → `on_call_end`.

### Behavior per category

| Category              | Action                                                                                                                                                               |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `human`               | Speak disclosure + `first_message`, proceed normally.                                                                                                                |
| `uncertain`           | Same as `human`.                                                                                                                                                     |
| `machine-ivr`         | Same as `human`; AMD has already started IVR navigation. The disclosure goes into the phone tree, which is harmless and covers a tree that later routes to a person. |
| `machine-vm`          | Hang up without speaking. `status=no_answer`, `end_reason=voicemail_reached`.                                                                                        |
| `machine-unavailable` | Hang up without speaking. `status=no_answer`, `end_reason=machine_unavailable`.                                                                                      |

No voicemail message is ever left. A partial line on someone's voicemail is
worse than silence, and leaving one would need a new `POST /calls` field.

## Outcome recording and billing

### New end reasons

`CallEndReason` (`core/hailhq/core/call_end_reasons.py`) gains:

- `VOICEMAIL_REACHED = "voicemail_reached"`
- `MACHINE_UNAVAILABLE = "machine_unavailable"`

Plus an alembic migration adding both to the `call_end_reason` Postgres ENUM.
`ALTER TYPE ... ADD VALUE` cannot run inside a transaction, so the migration
uses an `autocommit_block`.

Both map to the existing `no_answer` status. No `CallStatus`,
`TERMINAL_CALL_STATUSES`, OpenAPI, or CLI change — a machine picking up is
still "no person answered."

### Billing guard

`on_call_end` currently writes a `usage_events` row only when
`final_status == "completed"` (`agent.py:581`). A machine-answered call is
terminal as `no_answer`, so it would go unbilled — but the minutes were real.

Change the condition to `answered_at is not None or final_status == "completed"`.

The first clause is the new one: billing already runs from `answered_at`
(pickup), so "the SIP leg went active" is exactly the condition under which
there is something to bill. The second clause is the historical test, kept
deliberately — `mark_call_answered` can miss (a DB blip, a dropped
`sip.callStatus` event), legacy rows predate `answered_at`, and the
`recording_duration_ms` fallback has no pickup stamp at all; dropping it would
silently stop billing those completed calls. A genuine no-answer satisfies
neither clause and stays unbilled.

**Deliberate widening:** this also starts billing a call that answered and then
failed mid-conversation (`status=failed`). That is intended — those minutes
were consumed too.

## `send_dtmf` tool

### Why a Hail tool rather than LiveKit's

`AgentSession(ivr_detection=True)` normally injects LiveKit's own
`send_dtmf_events` tool for the whole call. But `AMD._run()` force-disables
session-level `ivr_detection` and takes over the IVR lifecycle
(`amd/detector.py:365`, unchanged in 1.6.6). With AMD on, LiveKit's DTMF tool
therefore exists **only** after a `machine-ivr` verdict — so a phone tree
reached mid-call, or after a human transfer, would be unpressable.

A Hail-registered tool is always available, uses only public API, and inherits
the registry's `body.tools` allowlist, per-org availability gate, unified
failure wrapper, and `call_events` `tool_call` logging.

### Shape

New `core/hailhq/core/agent_tools/send_dtmf.py`, one line added to
`registry.py`. Follows `end_call.py` exactly.

`ToolContext` gains:

```python
send_dtmf: Callable[[str], Awaitable[None]] | None
```

The voicebot builds this closure over
`ctx.room.local_participant.publish_dtmf`, the same way it builds `hangup`.
This keeps `core/` free of `livekit` imports, matching the existing capability-
handle pattern.

Spec:

- **Parameters:** `{"digits": {"type": "string"}}`, e.g. `"123#"`.
- **Validation:** characters restricted to `0-9`, `*`, `#`, `A-D`; max 32
  characters. A reject returns a speakable sentence, not an exception.
- **Codes:** RFC 4733 — `0-9` → `0-9`, `*` → 10, `#` → 11, `A-D` → 12-15.
- **Pacing:** 0.3s between digits, matching LiveKit's own
  `DEFAULT_DTMF_PUBLISH_DELAY`.
- **`risk_tier`:** `session_control`. Same as `end_call`, so the voicebot
  wrapper's `context.wait_for_playout()` runs first and the agent never sends
  tones over its own TTS.
- **`is_available`:** always `True`.
- **Returns:** a short spoken confirmation, or `SPOKEN_FALLBACK` on failure.

AMD's automatic IVR navigation still runs on top of this for calls that reach a
phone tree at pickup.

## Testing

**`voicebot/tests/test_amd.py`**

- Each of the five categories: hangup vs. proceed, `captured` status and
  `end_reason` stamped correctly, disclosure spoken or suppressed.
- `amd_result` call event written on every path.
- `run_amd` raising → returns `None` and the call proceeds normally.
- AMD constructed with the session's own `llm`/`stt` and the right
  `participant_identity`.

**`core/tests/test_send_dtmf.py`**

- Digit-to-code mapping across `0-9 * # A-D`.
- Validation rejects (empty, over-length, illegal characters) return speakable
  text.
- Digits are published in order.
- `ctx.send_dtmf is None` → speakable line, no raise.

**Billing**

- `answered_at` set + `status=no_answer` → `usage_events` row written.
- `answered_at` `None` → no row.

**Regression (dependency bump)**

- Existing `voicebot/`, `core/`, and `api/` suites pass on 1.6.6.
- `session.on("close")` reason mapping still produces the expected
  `CallEndReason` values.

## Documentation

`docs/architecture.md` voicebot section gains a short AMD + DTMF paragraph.

No `.env.example` change (no new env vars), no `openapi.yaml` regen, no CLI
codegen.

## Out of scope

- Leaving voicemail messages.
- A per-call AMD opt-out. AMD runs on every outbound call.
- A new `voicemail` call status.
- Inbound calls.
