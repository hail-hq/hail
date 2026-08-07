# Console Speechmatics BYO STT (PR 2a + 2b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an org select Speechmatics as its BYO STT provider in the console, including the `operating_point` (enhanced/standard) parameter.

**Architecture:** Two PRs. PR 2a (hail repo): `STTParams` accepts `operating_point` and the voicebot honors it for BYO Speechmatics rows. PR 2b (hail-website): the Providers page STT layer gains a provider select (currently gated off entirely), a Speechmatics option, and an operating-point select, mirroring the existing TTS/llm drawer patterns.

**Tech Stack:** hail: Python/pydantic v2/LiveKit plugin. hail-website: Next.js server actions + pure-helper modules, vitest.

**Design basis:** spec §7 + amendment in `docs/superpowers/specs/2026-07-29-multi-language-voicebot-design.md` (console is now the ONLY STT selector); file-level scout of `~/hail-website/app/console/calls/providers/` (2026-08-07).

## Global Constraints

- hail work on branch `feat/stt-operating-point` off `main` (repo `/Users/r/playground/hail`).
- hail-website work in a **git worktree** off `master` (repo `/Users/r/hail-website` has uncommitted unrelated work on `feat/competitive-pricing-page` — do not touch the main checkout). Branch `feat/console-speechmatics-stt`. Create via `git -C /Users/r/hail-website worktree add /Users/r/hail-website-wt-speechmatics -b feat/console-speechmatics-stt origin/master`.
- Conventional Commits. NEVER any Co-Authored-By / AI-attribution trailer.
- hail: `uv run pytest <pkg>/tests -q` from repo root; `uv run ruff check --fix` + `uvx black` before commits. hail-website: `pnpm test` (vitest), `pnpm lint` if present (check package.json scripts).
- Ship PR 2a before merging PR 2b's operating-point UI (a console PUT carrying `operating_point` 422s against today's backend). PR 2b's provider-select-only parts work against today's backend but ship together with the field for one review cycle.

---

### Task A1: hail — STTParams.operating_point + voicebot honors it

**Files:**

- Modify: `core/hailhq/core/provider_config.py` (STTParams, ~line 87)
- Modify: `voicebot/hailhq/voicebot/pipeline.py` (`build_stt` speechmatics branch)
- Test: `core/tests/schemas/test_voice_config_language.py` (STTParams cases live here from the earlier feature — check; else the file that tests STTParams), `voicebot/tests/test_pipeline_byo.py`

**Interfaces:**

- Produces: `STTParams.operating_point: Literal["enhanced", "standard"] | None = None`; BYO speechmatics rows with `params.operating_point == "standard"` build `speechmatics_plugin.STT` with `OperatingPoint.STANDARD`; default stays ENHANCED everywhere else (house instances included).

- [ ] **Step 1: Write the failing tests**

Core (put beside the existing `test_stt_params_accept_speechmatics`):

```python
def test_stt_params_operating_point() -> None:
    assert STTParams(provider="speechmatics").operating_point is None
    assert (
        STTParams(provider="speechmatics", operating_point="standard").operating_point
        == "standard"
    )
    with pytest.raises(ValidationError):
        STTParams(provider="speechmatics", operating_point="turbo")
```

Voicebot (`voicebot/tests/test_pipeline_byo.py`, beside `test_stt_org_speechmatics_key_used`; the file's `ResolvedLayer` idiom):

```python
def test_stt_org_speechmatics_operating_point_standard(captured_plugins) -> None:
    from livekit.plugins import speechmatics as speechmatics_plugin

    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={"operating_point": "standard"},
        fallback_enabled=False,
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt._stt_options.operating_point == speechmatics_plugin.OperatingPoint.STANDARD


def test_stt_org_speechmatics_operating_point_defaults_enhanced(
    captured_plugins,
) -> None:
    from livekit.plugins import speechmatics as speechmatics_plugin

    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics", api_key="sm-org-key", params={}, fallback_enabled=False
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt._stt_options.operating_point == speechmatics_plugin.OperatingPoint.ENHANCED
```

First verify `OperatingPoint.STANDARD` exists in the installed plugin
(`uv run python -c "from livekit.plugins.speechmatics import OperatingPoint; print(list(OperatingPoint))"`)
and that `_stt_options.operating_point` stores the enum (it stored the enum after the
2026-07-30 fix commit `9423faf` — re-verify). Adjust assertions to the installed reality.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest core/tests -q -k operating_point && uv run pytest voicebot/tests/test_pipeline_byo.py -q -k operating_point`
Expected: core FAILS (extra=forbid rejects the field); voicebot FAILS (standard not honored).

- [ ] **Step 3: Implement**

`core/hailhq/core/provider_config.py`:

```python
class STTParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepgram", "speechmatics"]
    model: str | None = None
    # Speechmatics transcription tier. None -> enhanced. Ignored on deepgram
    # rows (accepted-but-unused would violate "no hidden behavior", so the
    # validator below rejects it there).
    operating_point: Literal["enhanced", "standard"] | None = None

    @model_validator(mode="after")
    def _operating_point_is_speechmatics_only(self) -> STTParams:
        if self.provider != "speechmatics" and self.operating_point is not None:
            raise ValueError("operating_point only applies to speechmatics")
        return self
```

(`model_validator` is already imported in this module for `LLMParams`.)

`voicebot/hailhq/voicebot/pipeline.py`, speechmatics branch of `build_stt` — replace the hardcoded operating point:

```python
        operating_point = speechmatics_plugin.OperatingPoint.ENHANCED
        if org_matches and org.params.get("operating_point") == "standard":
            operating_point = speechmatics_plugin.OperatingPoint.STANDARD
        kwargs: dict[str, Any] = {
            "language": language or "en",
            "operating_point": operating_point,
        }
```

The house-fallback instance built from `house_kwargs` keeps whatever this sets minus
`api_key` — that is correct (a failover keeps the org's chosen tier only if the house
copy uses the same kwargs; keep the existing `house_kwargs = dict(kwargs)` behavior).

- [ ] **Step 4: Run the full core + voicebot suites**

Run: `uv run pytest core/tests -q` (6 pre-existing env failures in test_ses_email/test_twilio_sms are expected) and `uv run pytest voicebot/tests -q`
Expected: green apart from the known 6.

- [ ] **Step 5: Check whether the internal provider-config routes appear in openapi.yaml**

Run: `grep -n "operating_point\|providers/{layer}" openapi/openapi.yaml | head`
If the internal provider routes are in the public spec, regen it per `docs/contributing.md` (live-app dump) in this commit. If absent (internal-only routes), no regen.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check --fix core/hailhq/core/provider_config.py voicebot/hailhq/voicebot/pipeline.py core/tests voicebot/tests
uvx black core/hailhq/core/provider_config.py voicebot/hailhq/voicebot/pipeline.py
git add -A core/ voicebot/ openapi/ && git commit -m "feat(voicebot): honor speechmatics operating_point from BYO stt params"
```

---

### Task B1: hail-website — provider list + drawer UI

**Files (all under the worktree `/Users/r/hail-website-wt-speechmatics/app/console/calls/providers/`):**

- Modify: `drawer-state.ts:6-10`, `params.ts:24-26`, `ProvidersClient.tsx` (lines below)
- Test: `__tests__/drawer-state.test.ts`, `__tests__/params.test.ts`

**Interfaces:**

- Consumes: backend accepts `provider: "speechmatics"` today; `operating_point` after Task A1.
- Produces: STT drawer with provider select (deepgram default, speechmatics option) and an enhanced/standard select shown only for speechmatics.

- [ ] **Step 1: Write the failing tests**

`__tests__/drawer-state.test.ts:22` — change to:

```ts
expect(providersFor("stt")).toEqual(["deepgram", "speechmatics"]);
```

`__tests__/params.test.ts` — extend the stt case (mirror its existing style at lines 27-31):

```ts
it("stt speechmatics keeps operating_point; deepgram drops it", () => {
  expect(
    buildParams("stt", {
      provider: "speechmatics",
      model: "",
      operating_point: "standard",
    }),
  ).toEqual({ operating_point: "standard" });
  expect(
    buildParams("stt", {
      provider: "deepgram",
      model: "nova-3",
      operating_point: "standard",
    }),
  ).toEqual({ model: "nova-3" });
});
```

(Adapt the form-object shape to `buildParams`' actual signature — read `params.ts` first; the scout says the stt branch currently only reads `form.model`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/r/hail-website-wt-speechmatics && pnpm install && pnpm test`
Expected: the two edited specs FAIL; rest green.

- [ ] **Step 3: Implement**

- `drawer-state.ts:9`: stt returns `["deepgram", "speechmatics"]` — **deepgram stays first** (`initDraft` defaults to `providersFor(layer)[0]`; ordering preserves existing orgs' default).
- `params.ts` stt branch: keep `put("model", form.model)`; add `operating_point` only when `form.provider === "speechmatics"` (mirror the llm `base_url` guard pattern at ~line 19).
- `ProvidersClient.tsx`:
  - Line ~392: change the gate `{layer !== "stt" && (` to `{providersFor(layer).length > 1 && (` so STT now renders the provider select.
  - Line ~13 `LAYERS` meta: `providerLabel: "Deepgram"` → `"Deepgram / Speechmatics"`.
  - `Draft` type (~18-32): add `operating_point: string;`.
  - `initDraft` (~39-56): initialize from `row?.params?.operating_point ?? "enhanced"`.
  - New field after the tts `voice_id` block (~449), gated `{layer === "stt" && draft.provider === "speechmatics" && (…)}`: a `div.c-fld` (mirror the voice_id markup) containing a `<select>` (mirror the provider select markup) with options `enhanced` / `standard`, bound to `draft.operating_point`.
  - `onSave` (~137) and `onTest` (~189) inline params objects: add `operating_point: draft.operating_point`.
  - `TEST_AFFECTING_KEYS` (~382): add `"operating_point"` (else a stale test-✓ survives a tier change).
  - `formatParams` whitelist (~328): add `"operating_point"` so the ledger shows it.
  - `providerOptionLabel` (~548-556): add `if (p === "speechmatics") return "Speechmatics";`.

- [ ] **Step 4: Run tests + typecheck**

Run: `pnpm test` and the repo's typecheck/lint scripts (check `package.json` — likely `pnpm lint` / `tsc --noEmit` via a script). All green.

- [ ] **Step 5: Commit**

```bash
git add app/console/calls/providers/ && git commit -m "feat(console): speechmatics BYO stt provider with operating point"
```

---

### Task B2: manual smoke against local hail backend (verification only)

- [ ] **Step 1:** From the hail repo (branch with Task A1 merged or checked out): start Postgres + API per CLAUDE.md dev commands. From the worktree: `pnpm dev`. In the console Providers page: STT layer now shows the provider select; choose Speechmatics, paste a test key (or a dummy — expect the validation endpoint to return invalid, which still proves the wire-through), pick `standard`, Save. Confirm: no 422 from the PUT, ledger row shows `speechmatics` + `operating_point`.
- [ ] **Step 2:** Record findings in the task report. If local stack cannot boot (missing env), record exactly what blocked and mark this task deferred for the user — do NOT fake the smoke.

---

### Final: whole-branch reviews

One review per repo diff (hail: `main..feat/stt-operating-point`; website worktree: `master..feat/console-speechmatics-stt`), then hand both branches to the user for push/PR (never push or create PRs).

---

## Part C — owner rulings on the four product calls (2026-08-07)

Rulings: (1) = option c, (2) = option b, (3) = accept/no work, (4) = leave + startup warning.
Unifying rule for C1: **`fallback_enabled` on a BYO row is consent to house-provider rerouting; without it, a capability mismatch is an error, never a silent reroute.**

### Task C1: consent-gated STT/TTS rerouting

**Files:**

- Modify: `api/hailhq/api/routes/calls.py` (the language/provider gate)
- Modify: `voicebot/hailhq/voicebot/pipeline.py` (`build_session` STT degrade branch; `build_tts` org branch)
- Test: `api/tests/test_calls_api.py`, `voicebot/tests/test_pipeline.py`, `voicebot/tests/test_pipeline_byo.py`

**Interfaces:**

- Consumes: `SUPPORTED_LANGUAGES`, `tts_providers_for` (existing); `OrgProviderConfig.fallback_enabled` column (existing); `ResolvedLayer.fallback_enabled` (existing).
- Produces: API — 422 when an org BYO row (STT or TTS) cannot serve the requested language AND `fallback_enabled` is false; allowed when true. Voicebot — with consent, STT degrades to house Deepgram (as today) and TTS skips the incapable BYO instance (house chain only); without consent, `ProviderKeyError` (defense-in-depth behind the API gate).

- [ ] **Step 1: Write the failing API tests** (mirror the existing gate tests' fixtures exactly; seed `OrgProviderConfig` the way `test_byo_elevenlabs_tts_with_cartesia_only_language_422` does):

```python
async def test_byo_stt_incapable_language_without_fallback_422(...):
    # org row: layer="stt", provider="speechmatics", fallback_enabled=False
    # POST language="gu" -> 422, message names speechmatics and gu
async def test_byo_stt_incapable_language_with_fallback_201(...):
    # same row but fallback_enabled=True -> 201
async def test_byo_tts_incapable_language_with_fallback_201(...):
    # org row: layer="tts", provider="elevenlabs", fallback_enabled=True
    # POST language="th" -> 201 (was 422 before this task)
```

Keep the existing `fallback_enabled=False` TTS 422 test passing (seed must set the flag explicitly if the column default differs).

- [ ] **Step 2: Write the failing voicebot tests**:

```python
def test_build_tts_org_incapable_with_fallback_uses_house_only(...):
    # org elevenlabs row fallback_enabled=True, language="th" ->
    # result contains ONLY cartesia instance(s), no elevenlabs
def test_build_tts_org_incapable_without_fallback_raises(...):
    # same but fallback_enabled=False -> ProviderKeyError
def test_session_org_stt_incapable_without_fallback_raises(...):
    # org speechmatics row fallback_enabled=False, language="gu" -> ProviderKeyError
def test_session_org_stt_incapable_with_fallback_degrades(...):
    # fallback_enabled=True -> deepgram STT, warning logged (existing behavior)
```

- [ ] **Step 3: Run all four files' new tests, verify failures**
- [ ] **Step 4: Implement** — API gate: both checks become `row is not None and row.provider not in caps.<layer> and not row.fallback_enabled`. Voicebot `build_session`: in the degrade branch, when the incapable provider came from the org row and `not org_stt.fallback_enabled`, raise `ProviderKeyError` naming provider+language. Voicebot `build_tts`: before building the BYO instance, if `language is not None and org.provider not in tts_providers_for(language)`: with consent → log + return the house chain (reuse the existing house-instances path incl. its empty-chain ProviderKeyError); without → raise `ProviderKeyError`. Update the gate's asymmetry comment (both layers now share the consent rule).
- [ ] **Step 5: Full api + voicebot suites green; lint; commit** (`feat: consent-gated rerouting for BYO stt/tts capability mismatches`).

### Task C2: voicebot startup capability warning

**Files:**

- Modify: `voicebot/hailhq/voicebot/main.py` (log before `cli.run_app`), helper in `voicebot/hailhq/voicebot/pipeline.py`
- Test: `voicebot/tests/test_pipeline.py`

**Interfaces:**

- Produces: `startup_capability_warnings() -> list[str]` in pipeline.py — pure function of `settings`: (a) when `cartesia_api_key` empty: one message listing the supported languages with no usable house TTS given configured keys; (b) when `speechmatics_api_key` empty: one message that the 22 speechmatics-routed languages fall back to Deepgram + VAD turn detection. `main()` logs each at WARNING.

- [ ] **Step 1: failing tests** — monkeypatch settings combos (both keys set → `[]`; cartesia empty + eleven set → message lists exactly the 7 Cartesia-only codes; speechmatics empty → the 22-language message).
- [ ] **Step 2: implement helper + wire into `main()`** (import cost: pipeline already imported transitively by agent; keep `main.py` lean — import the helper only).
- [ ] **Step 3: voicebot suite green; lint; commit** (`feat(voicebot): startup warning for unservable languages`).
