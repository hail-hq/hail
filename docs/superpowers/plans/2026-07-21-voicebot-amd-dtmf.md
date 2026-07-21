# Plan — voicebot AMD + agent DTMF

Spec: [2026-07-21-voicebot-amd-dtmf-design.md](../specs/2026-07-21-voicebot-amd-dtmf-design.md)

Status legend: `[ ]` todo · `[x]` done

## 1. Dependency bump to livekit-agents 1.6.6

- [x] `uv lock --upgrade-package …` for `livekit-agents` + the 8 `livekit-plugins-*`.
      No `pyproject.toml` edit (`>=1.5,<2` already allows it).
      Files: `uv.lock`
- [x] Baseline (1.5.6) suites recorded, then re-run on 1.6.6:
      `cd voicebot && uv run pytest -q`, `cd core && …`, `cd api && …`
- [x] Re-verify the two flagged upstream changes: - `CloseReason` is still `str, Enum` with `error` / `job_shutdown` →
      `_on_session_close`'s `reason == "error"` comparison still holds. - `silero.VAD.load()` (now ONNX) still loads in-process.

## 2. AMD module

- [x] New `voicebot/hailhq/voicebot/amd.py` exporting
      `MACHINE_HANGUP_CATEGORIES`, `amd_end_reason`, `run_amd`.
      `run_amd` builds `AMD(session, llm=…, stt=…, participant_identity=
    f"caller-{call_id}", detection_options={"no_speech_threshold": 6.0},
    suppress_compatibility_warning=True)` and returns `await detector.execute()`
      inside `async with`; any exception → log + `None`.
- [x] Verify: `cd voicebot && uv run pytest tests/test_amd.py -q`

## 3. New CallEndReason values + migration

- [x] `core/hailhq/core/call_end_reasons.py`: add `VOICEMAIL_REACHED`,
      `MACHINE_UNAVAILABLE`.
- [x] `api/migrations/versions/0038_amd_call_end_reasons.py` — `autocommit_block` + `ALTER TYPE call_end_reason ADD VALUE IF NOT EXISTS …` for both
      (mirrors `0024_provider_key_error_reason.py`).
- [x] Verify: `cd api && uv run pytest -q` (fixtures build the enum from the
      Python `StrEnum`, so a mismatch surfaces there).

## 4. Entrypoint wiring

- [x] `voicebot/hailhq/voicebot/agent.py` `entrypoint`: between `session.start()`
      and `speak_greeting`, run AMD, write the `amd_result` call event on every
      path, and hang up (delete_room + shutdown) for the two machine categories.

## 5. Billing guard

- [x] `agent.py` `on_call_end`: bill when `answered_at is not None` instead of
      `final_status == "completed"`.
- [x] Existing billing tests that bill off `started_at` alone must be updated to
      stamp `answered_at` — the new contract is "the SIP leg went active".

## 6. send_dtmf tool

- [x] `core/hailhq/core/agent_tools/spec.py`: `ToolContext.send_dtmf` field.
- [x] `core/hailhq/core/agent_tools/send_dtmf.py` — validation, RFC 4733 codes,
      0.3s pacing, `session_control` tier, always available.
- [x] `core/hailhq/core/agent_tools/registry.py`: one line.
- [x] `voicebot/hailhq/voicebot/tools.py`: accept + thread a `send_dtmf` handle.
- [x] `agent.py`: `make_agent_send_dtmf(ctx)` closure over
      `ctx.room.local_participant.publish_dtmf`; passed through
      `build_tools_safely`.
- [x] Verify: `cd core && uv run pytest tests/test_send_dtmf.py tests/test_agent_tools.py -q`
      and `cd voicebot && uv run pytest tests/test_tools.py -q`

## 7. Tests

- [x] `voicebot/tests/test_amd.py` — five categories, `amd_result` event always
      written, `run_amd` failure → `None` + call proceeds, AMD constructed with
      the session's own `llm`/`stt` and the right `participant_identity`.
- [x] `core/tests/test_send_dtmf.py` — code mapping, rejects, ordering,
      `send_dtmf is None`.
- [x] Billing assertions in `voicebot/tests/test_agent.py`.

## 8. Docs

- [x] `docs/architecture.md` voicebot paragraph.

## 9. Full verification

- [x] `cd voicebot && uv run pytest`
- [x] `cd core && uv run pytest`
- [x] `cd api && uv run pytest`
- [x] `uv run ruff check --fix . && uv run black .`
- [x] `uv run mypy` per package

## Deviations from the spec

1. **Shutdown-callback ordering.** The spec's entrypoint snippet `return`s from
   the AMD hangup path before `ctx.add_shutdown_callback(_shutdown)` is
   registered, which would leave the Call row unfinalized and the pool number
   leaked. The registration (and `room_name`) moved above the AMD block; the
   soft-cap task still starts only after the greeting.
2. **`run_amd` guards on `session.stt is None`** in addition to the spec's
   `isinstance` narrowing of `session.llm` — `AMD(stt=None)` is "given" as far
   as `is_given` is concerned and would fail downstream.
3. **`test_on_call_end_inserts_usage_event` updated** to stamp `answered_at`.
   It billed off `started_at` alone, which the new billing guard deliberately
   no longer does. Intended consequence of the spec's change, not a workaround.
4. **CI does not run mypy** (`.github/workflows/ci.yml` runs ruff, black, and
   pytest only), and `uv run mypy .` per package fails on main already with
   "Source file found twice under different module names". Type-checked the
   changed files with `--explicit-package-bases`; no new errors.
