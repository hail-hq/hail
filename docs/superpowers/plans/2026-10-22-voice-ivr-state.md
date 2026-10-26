# Phone menu state implementation plan

**Goal:** Correct the premature human handoff, menu narration, and unsupported
unavailable verdicts observed in outbound calls.

**Architecture:** Keep an explicit phone-menu state on the voice agent. Classify
completed remote speech with a bounded, tool-only request before releasing the
greeting. Keep navigation instructions on every menu turn and suppress menu speech
at the TTS and transcript boundaries. Require a second classification before an
unavailable greeting ends the call. Existing uncertain-pickup PR #224 and model
chain PR #238 remain independent.

**Constraints:** No production changes, database writes, real phone calls, merging,
or model configuration changes. Use an isolated local test database. A VAD event
alone is not evidence of a human. After speech ends, allow eight seconds for a
usable completed turn; speech that stays unclassified (uncertain, or no
transcript) then greets, as LiveKit's AMD guide treats uncertain as a human.
Only an explicit voicemail or unavailable verdict hangs up. After a recorded
menu, hold or queue line (including the pickup), VAD speech with no transcript
does not start the wait until a key is pressed. Keep long menus alive while
speech/transcription continues. Once a greeting or hang-up starts, late input
cannot cancel it.

## Task 1: Explicit menu state

- [x] Add failing behavioral tests for a multi-stage menu, human greeting,
  voicemail after a menu, classifier failure, cancellation, and multilingual text.
- [x] Add `PhoneMenu.observe(text)` and bounded speech classification in
  `voicebot/hailhq/voicebot/ivr.py`.
- [x] Integrate state into the agent turn and TTS hooks; replace the keypress/VAD
  handoff with a classified handoff, and suppress unspoken transcript entries.
- [x] Run targeted voicebot tests against the isolated database.

## Task 2: False terminal verdict and caller behavior

- [x] Add failing coverage for a business greeting incorrectly labelled unavailable.
- [x] Recheck unavailable verdicts before hangup; preserve explicit voicemail.
- [x] Keep complete-menu selection and silence instructions on each menu turn.
  Correct the outbound support-role closing while preserving inbound service rules.
- [x] Run the full voicebot and core suites, formatting, and lint.

## Review focus

- No human transition from keypresses or audio energy alone.
- Classifier errors or stale results cannot unlock speech or hang up a human.
- Human greetings without a prior keypress still release the agent.
- Menu narration cannot reach audio or appear as spoken transcript.
- Existing voicemail, inbound, opening-order, and interruption behavior remains covered.

## Delivery

- [x] Review the complete diff and prepare the task branch for a PR with validation and limits.
- [x] Remove this task's local test container. Keep the open PR's worktree.

## Validation

- Core: 1,275 passed. Voicebot: 423 passed (rerun 2026-10-23).
- Ruff 0.16.10 and Black 26.10.0: whole-repository checks passed.
- Live text replay: nine classification cases each on Haiku 5.5 and GPT-4.1;
  four menu/closing cases on GPT-4.1. All final cases passed.
- Review regressions cover database latency, terminal audio-recheck verdicts,
  continuous menus, interim-only input, late progress, and initiated shutdown.
- No live phone call or production deployment. Unclassified speech greets after
  eight seconds, except untranscribed audio after a recorded menu or hold line
  with no keypress since; this does not add multilingual audio recovery.

## Atlantis regression verification (2026-10-23)

- Call `435dd50f` received an unavailable verdict
  for its Atlantis business welcome before the reservation menu arrived.
- Added a regression through the real `run_amd` verification and entrypoint:
  the exact recorded welcome stays silent and connected, and the later menu
  remains navigable. Covers IVR, uncertain, and failed verification requests.
- All three cases fail at room deletion when the unavailable verification is
  disabled, and pass with the existing fix restored. No additional production
  behavior change was needed for this call.
- GPT-4.1 text replay: the pickup and verification prompts each correctly
  classify the exact welcome, the later menu, a full mailbox, voicemail, and a
  live reservations greeting (10/10 cases). Audio and PSTN remain untested.
