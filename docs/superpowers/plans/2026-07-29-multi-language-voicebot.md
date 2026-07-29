# Multi-Language Voicebot Implementation Plan (hail repo, PR 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support 39 call languages via `voice_config.language`, adding Speechmatics as a second STT provider with automatic per-language routing and turn-detection selection.

**Architecture:** A new `core/hailhq/core/languages.py` is the single source of truth for per-language STT/TTS/turn-detection capabilities. The API validates language/provider combos at request time (422); the voicebot resolves the STT provider (per-call pin > org BYO row > auto-route) and picks the turn-detection strategy (LiveKit semantic model / Speechmatics STT-driven / VAD-only). House TTS drops the ElevenLabs fallback for languages ElevenLabs can't speak.

**Tech Stack:** Python 3.13 / pydantic v2 / FastAPI / LiveKit Agents (`livekit-plugins-speechmatics`, `livekit-plugins-turn-detector`) / Go CLI via oapi-codegen.

**Spec:** `docs/superpowers/specs/2026-07-29-multi-language-voicebot-design.md` — read it first.

## Global Constraints

- Branch: `feat/multi-language-voicebot` off `main` (gitflow prefix rule; never commit to main).
- **Ask the user before the first commit of the session** (their standing rule: no autonomous commits). Once approved for this session, the per-task commit steps below apply.
- Commit messages: Conventional Commits. **Never** add `Co-Authored-By: Claude` or any AI-attribution trailer.
- New env vars go into `.env.example` in the same commit as the code that reads them. 🛑 Deploy TODO (report at the end, do not do it): set real `SPEECHMATICS_API_KEY` in the prod VM `.env` before deploying.
- Python dep sync: **only** `uv sync --all-packages --all-extras` at the repo root — never `uv sync --extra dev` inside a subpackage (it prunes the shared venv).
- Run Python tests from the repo root: `uv run pytest <path>`. Lint: `uv run ruff check --fix <paths>` and `uv run black <paths>` before each commit.
- `openapi/openapi.yaml` is regenerated from the live app, never hand-edited; regen in the same PR as the schema change (Task 9).
- hail-website console work (Speechmatics BYO UI) is **PR 2 with its own plan** — not in this plan.

## Language data (from the verified research; encode exactly)

39 supported codes. All have Deepgram STT and Cartesia TTS. Deviations:

- Semantic-turn languages (LiveKit MultilingualModel, 14): `en es fr de it pt nl zh ja ko id tr ru hi`
- No Speechmatics STT (3): `gu kn te`
- No ElevenLabs TTS (7): `bn gu he kn te th mr`
- Excluded from support entirely (capability mismatch): `ka ml pa`

---

### Task 1: Language capability matrix in core

**Files:**
- Create: `core/hailhq/core/languages.py`
- Test: `core/tests/test_languages.py`

**Interfaces:**
- Produces: `Language` (Literal of 39 codes), `LanguageCaps` (frozen dataclass: `name: str`, `stt: frozenset[str]`, `tts: frozenset[str]`, `semantic_turn: bool`), `SUPPORTED_LANGUAGES: dict[str, LanguageCaps]`, `default_stt_for(language: str | None) -> str`, `resolve_stt_provider(requested: str, org_provider: str | None, language: str | None) -> str`, `tts_providers_for(language: str | None) -> frozenset[str]`, `turn_mode_for(language: str | None, stt_provider: str) -> str` (returns `"semantic" | "stt" | "vad"`).

- [ ] **Step 1: Write the failing tests**

Create `core/tests/test_languages.py`:

```python
"""Tests for the language capability matrix and routing helpers."""

from __future__ import annotations

from typing import get_args

from hailhq.core.languages import (
    Language,
    SUPPORTED_LANGUAGES,
    default_stt_for,
    resolve_stt_provider,
    tts_providers_for,
    turn_mode_for,
)


def test_literal_matches_matrix_keys() -> None:
    assert set(get_args(Language)) == set(SUPPORTED_LANGUAGES)


def test_matrix_has_39_languages_and_excludes_mismatches() -> None:
    assert len(SUPPORTED_LANGUAGES) == 39
    for excluded in ("ka", "ml", "pa"):
        assert excluded not in SUPPORTED_LANGUAGES


def test_every_language_has_deepgram_and_cartesia() -> None:
    for caps in SUPPORTED_LANGUAGES.values():
        assert "deepgram" in caps.stt
        assert "cartesia" in caps.tts


def test_default_stt_routing() -> None:
    assert default_stt_for(None) == "deepgram"  # English default
    assert default_stt_for("en") == "deepgram"  # semantic-turn language
    assert default_stt_for("de") == "deepgram"  # semantic-turn language
    assert default_stt_for("da") == "speechmatics"  # outside the 14
    assert default_stt_for("sv") == "speechmatics"
    assert default_stt_for("gu") == "deepgram"  # speechmatics can't do gu


def test_resolve_stt_provider_precedence() -> None:
    # per-call pin wins over org row and auto
    assert resolve_stt_provider("deepgram", "speechmatics", "da") == "deepgram"
    # org BYO row wins over auto
    assert resolve_stt_provider("auto", "speechmatics", "en") == "speechmatics"
    # auto with no org row -> routed
    assert resolve_stt_provider("auto", None, "da") == "speechmatics"
    assert resolve_stt_provider("auto", None, None) == "deepgram"


def test_tts_providers_trim() -> None:
    assert tts_providers_for(None) == frozenset({"cartesia", "elevenlabs"})
    assert tts_providers_for("fr") == frozenset({"cartesia", "elevenlabs"})
    assert tts_providers_for("th") == frozenset({"cartesia"})  # no elevenlabs


def test_turn_mode_selection() -> None:
    assert turn_mode_for(None, "deepgram") == "semantic"
    assert turn_mode_for("en", "speechmatics") == "semantic"  # 14 beats stt
    assert turn_mode_for("da", "speechmatics") == "stt"
    assert turn_mode_for("da", "deepgram") == "vad"  # pinned away from sm
    assert turn_mode_for("gu", "deepgram") == "vad"  # nothing better exists
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest core/tests/test_languages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hailhq.core.languages'`

- [ ] **Step 3: Write the implementation**

Create `core/hailhq/core/languages.py`:

```python
"""Language capability matrix for the voice pipeline.

Single source of truth for which call languages Hail supports and which
STT/TTS provider serves each one. Derived from official provider docs on
2026-07-29 (research spec:
``docs/superpowers/specs/2026-07-29-multi-language-voicebot-design.md``):

* Deepgram nova-3 streaming languages —
  https://developers.deepgram.com/docs/models-languages-overview
* Speechmatics real-time languages —
  https://docs.speechmatics.com/speech-to-text/languages
* Cartesia sonic-3.5 languages —
  https://docs.cartesia.ai/build-with-cartesia/tts-models/latest
* ElevenLabs eleven_turbo_v2_5 languages —
  https://elevenlabs.io/docs/models
* LiveKit MultilingualModel turn-detector languages —
  https://docs.livekit.io/agents/build/turns/turn-detector/

Supported = (nova-3 ∪ Speechmatics STT) ∩ sonic-3.5. Excluded for
capability mismatch (STT exists only on Whisper-based models Hail doesn't
run): ka, ml, pa.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

__all__ = [
    "Language",
    "LanguageCaps",
    "SUPPORTED_LANGUAGES",
    "default_stt_for",
    "resolve_stt_provider",
    "tts_providers_for",
    "turn_mode_for",
]

# Keep in sync with SUPPORTED_LANGUAGES — test_literal_matches_matrix_keys
# guards the pairing. A Literal (not a StrEnum) so pydantic renders a plain
# string enum in the OpenAPI spec.
Language = Literal[
    "ar", "bg", "bn", "cs", "da", "de", "el", "en", "es", "fi",
    "fr", "gu", "he", "hi", "hr", "hu", "id", "it", "ja", "kn",
    "ko", "mr", "ms", "nl", "no", "pl", "pt", "ro", "ru", "sk",
    "sv", "ta", "te", "th", "tl", "tr", "uk", "vi", "zh",
]

_NAMES: dict[str, str] = {
    "ar": "Arabic", "bg": "Bulgarian", "bn": "Bengali", "cs": "Czech",
    "da": "Danish", "de": "German", "el": "Greek", "en": "English",
    "es": "Spanish", "fi": "Finnish", "fr": "French", "gu": "Gujarati",
    "he": "Hebrew", "hi": "Hindi", "hr": "Croatian", "hu": "Hungarian",
    "id": "Indonesian", "it": "Italian", "ja": "Japanese", "kn": "Kannada",
    "ko": "Korean", "mr": "Marathi", "ms": "Malay", "nl": "Dutch",
    "no": "Norwegian", "pl": "Polish", "pt": "Portuguese", "ro": "Romanian",
    "ru": "Russian", "sk": "Slovak", "sv": "Swedish", "ta": "Tamil",
    "te": "Telugu", "th": "Thai", "tl": "Tagalog", "tr": "Turkish",
    "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese",
}

# LiveKit MultilingualModel semantic turn detector coverage.
_SEMANTIC_TURN = frozenset(
    {"en", "es", "fr", "de", "it", "pt", "nl", "zh", "ja", "ko",
     "id", "tr", "ru", "hi"}
)
# Languages Speechmatics real-time STT does NOT cover (nova-3 only).
_NO_SPEECHMATICS = frozenset({"gu", "kn", "te"})
# Languages ElevenLabs eleven_turbo_v2_5 does NOT cover (Cartesia only).
_NO_ELEVENLABS = frozenset({"bn", "gu", "he", "kn", "te", "th", "mr"})


@dataclasses.dataclass(frozen=True)
class LanguageCaps:
    name: str
    stt: frozenset[str]
    tts: frozenset[str]
    semantic_turn: bool


SUPPORTED_LANGUAGES: dict[str, LanguageCaps] = {
    code: LanguageCaps(
        name=_NAMES[code],
        stt=(
            frozenset({"deepgram"})
            if code in _NO_SPEECHMATICS
            else frozenset({"deepgram", "speechmatics"})
        ),
        tts=(
            frozenset({"cartesia"})
            if code in _NO_ELEVENLABS
            else frozenset({"cartesia", "elevenlabs"})
        ),
        semantic_turn=code in _SEMANTIC_TURN,
    )
    for code in _NAMES
}


def default_stt_for(language: str | None) -> str:
    """Auto-route: Deepgram wherever the semantic turn detector works
    (or no language is set), Speechmatics where it doesn't and Speechmatics
    covers the language, Deepgram otherwise."""
    if language is None:
        return "deepgram"
    caps = SUPPORTED_LANGUAGES[language]
    if caps.semantic_turn:
        return "deepgram"
    if "speechmatics" in caps.stt:
        return "speechmatics"
    return "deepgram"


def resolve_stt_provider(
    requested: str, org_provider: str | None, language: str | None
) -> str:
    """Precedence mirrors the pipeline's layers: per-call pin > org BYO
    standing choice > language auto-routing."""
    if requested != "auto":
        return requested
    if org_provider is not None:
        return org_provider
    return default_stt_for(language)


def tts_providers_for(language: str | None) -> frozenset[str]:
    if language is None:
        return frozenset({"cartesia", "elevenlabs"})
    return SUPPORTED_LANGUAGES[language].tts


def turn_mode_for(language: str | None, stt_provider: str) -> str:
    """Pick the turn-detection strategy for one call.

    "semantic" — LiveKit MultilingualModel (best; 14 languages, any STT).
    "stt" — Speechmatics drives end-of-turn from the transcript stream.
    "vad" — silence-gap only; the floor when nothing better exists.
    """
    if language is None or SUPPORTED_LANGUAGES[language].semantic_turn:
        return "semantic"
    if stt_provider == "speechmatics":
        return "stt"
    return "vad"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest core/tests/test_languages.py -v`
Expected: 7 passed

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix core/hailhq/core/languages.py core/tests/test_languages.py
uv run black core/hailhq/core/languages.py core/tests/test_languages.py
git add core/hailhq/core/languages.py core/tests/test_languages.py
git commit -m "feat(core): language capability matrix and stt routing helpers"
```

---

### Task 2: Widen the schemas — language enum, `stt: "auto"`, BYO speechmatics

**Files:**
- Modify: `core/hailhq/core/schemas.py:126-145` (class `VoiceConfig`)
- Modify: `core/hailhq/core/provider_config.py:86-89` (class `STTParams`)
- Test: `core/tests/schemas/test_voice_config_language.py` (create)

**Interfaces:**
- Consumes: `Language` from Task 1.
- Produces: `VoiceConfig.language: Language | None`, `VoiceConfig.stt: Literal["auto", "deepgram", "speechmatics"]` default `"auto"`, `STTParams.provider: Literal["deepgram", "speechmatics"]`.

- [ ] **Step 1: Write the failing tests**

Create `core/tests/schemas/test_voice_config_language.py`:

```python
"""VoiceConfig language enum + stt selector validation."""

from __future__ import annotations

import pytest
from hailhq.core.provider_config import STTParams
from hailhq.core.schemas import VoiceConfig
from pydantic import ValidationError


def test_supported_language_accepted() -> None:
    assert VoiceConfig(language="da").language == "da"


def test_unsupported_language_rejected() -> None:
    with pytest.raises(ValidationError):
        VoiceConfig(language="ka")  # excluded: Whisper-only STT
    with pytest.raises(ValidationError):
        VoiceConfig(language="xx")


def test_stt_defaults_to_auto_and_accepts_speechmatics() -> None:
    assert VoiceConfig().stt == "auto"
    assert VoiceConfig(stt="speechmatics").stt == "speechmatics"
    with pytest.raises(ValidationError):
        VoiceConfig(stt="whisper")


def test_openapi_schema_exposes_language_enum() -> None:
    schema = VoiceConfig.model_json_schema()
    prop = schema["properties"]["language"]
    # pydantic renders Optional[Literal[...]] as anyOf [enum, null]
    enums = [e for e in prop.get("anyOf", []) if "enum" in e]
    assert enums and len(enums[0]["enum"]) == 39


def test_stt_params_accept_speechmatics() -> None:
    assert STTParams(provider="speechmatics").provider == "speechmatics"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest core/tests/schemas/test_voice_config_language.py -v`
Expected: FAIL — `language="ka"` currently passes the loose pattern; `stt="auto"` currently rejected by `Literal["deepgram"]`.

- [ ] **Step 3: Implement the schema changes**

In `core/hailhq/core/schemas.py`, add the import near the other `hailhq.core` imports:

```python
from hailhq.core.languages import Language
```

Replace the `VoiceConfig.stt` and `VoiceConfig.language` fields (keep `tts`/`vad`/`turn_detection`/`voice_id` as they are):

```python
    stt: Literal["auto", "deepgram", "speechmatics"] = Field(
        default="auto",
        description=(
            "STT provider for the call. 'auto' (default) routes by "
            "language: Deepgram where LiveKit's semantic turn detector "
            "covers the language, Speechmatics elsewhere. An explicit "
            "value pins the provider (rejected with 422 if it does not "
            "support the requested language)."
        ),
    )
```

```python
    language: Language | None = Field(
        default=None,
        description=(
            "Spoken language for the call as a lowercase ISO 639-1 code "
            "(e.g. 'da'). One of the 39 supported codes — see "
            "docs/languages.md. Applied to STT, TTS, and turn detection. "
            "Omitted: the providers' defaults (English)."
        ),
    )
```

In `core/hailhq/core/provider_config.py`, widen `STTParams`:

```python
class STTParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepgram", "speechmatics"]
    model: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest core/tests/test_languages.py core/tests/schemas/ -v`
Expected: all pass. Also run the full core suite to catch anything relying on `stt == "deepgram"` default: `uv run pytest core/tests -q` — fix any test asserting the old default by updating it to `"auto"` (that default change is intentional per the spec).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix core/hailhq/core/schemas.py core/hailhq/core/provider_config.py core/tests/schemas/test_voice_config_language.py
uv run black core/hailhq/core/schemas.py core/hailhq/core/provider_config.py core/tests/schemas/test_voice_config_language.py
git add -A core/
git commit -m "feat(core): language enum on VoiceConfig, stt auto selector, speechmatics BYO param"
```

---

### Task 3: Speechmatics settings + env example

**Files:**
- Modify: `core/hailhq/core/config.py:29-31` (voice-pipeline settings block)
- Modify: `.env.example` (voice-provider section, near `DEEPGRAM_API_KEY`)

**Interfaces:**
- Produces: `settings.speechmatics_api_key: str` (empty default).

- [ ] **Step 1: Add the setting**

In `core/hailhq/core/config.py`, in the `# Voice pipeline` block after `eleven_api_key`:

```python
    speechmatics_api_key: str = ""
```

- [ ] **Step 2: Add to `.env.example`**

Find the Deepgram entry (`DEEPGRAM_API_KEY`) and add below it, matching the file's comment style:

```bash
# Speechmatics — STT for languages outside the semantic turn detector's
# coverage (auto-routed) and for explicit voice_config.stt="speechmatics".
# Leave empty to route every call to Deepgram. https://portal.speechmatics.com/
SPEECHMATICS_API_KEY=
```

- [ ] **Step 3: Verify settings load**

Run: `uv run pytest core/tests/test_config.py -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add core/hailhq/core/config.py .env.example
git commit -m "feat(core): SPEECHMATICS_API_KEY setting"
```

---

### Task 4: Voicebot — Speechmatics plugin dep + verified API surface

**Files:**
- Modify: `voicebot/pyproject.toml:12-21` (dependencies list)
- Modify: `voicebot/hailhq/voicebot/pipeline.py:29-43` (the "API surface verified" docstring block)

**Interfaces:**
- Produces: importable `livekit.plugins.speechmatics` with verified constructor kwargs, recorded in the pipeline docstring for later tasks.

- [ ] **Step 1: Add the dependency**

In `voicebot/pyproject.toml`, after the `livekit-plugins-silero` line:

```toml
    "livekit-plugins-speechmatics>=1.5,<2",
```

- [ ] **Step 2: Sync from the repo root**

Run: `uv sync --all-packages --all-extras`
Expected: resolves and installs `livekit-plugins-speechmatics` (and its `speechmatics-rt` transitive dep) without downgrading other pins.

- [ ] **Step 3: Verify the plugin's constructor API against installed source**

This repo's convention (see the pipeline module docstring) is to verify plugin APIs against the installed version before use — the research (2026-07-29) says `speechmatics.STT` accepts `language`, `operating_point`, `api_key`, `turn_detection_mode` (enum `TurnDetectionMode` with `EXTERNAL`/`FIXED`/`ADAPTIVE`/`SMART_TURN`), `end_of_utterance_silence_trigger`; confirm:

Run: `uv run python -c "import inspect; from livekit.plugins import speechmatics; print(inspect.signature(speechmatics.STT.__init__))"`
and: `uv run python -c "from livekit.plugins import speechmatics; print([m for m in dir(speechmatics) if 'Turn' in m])"`

Record findings. **Decision rule:** use `turn_detection_mode=TurnDetectionMode.ADAPTIVE`; if ADAPTIVE requires an extra package (import error mentioning `speechmatics-voice[smart]` or similar), fall back to `TurnDetectionMode.FIXED` with `end_of_utterance_silence_trigger=0.7` — both are all-language and both drive `turn_detection="stt"`. If kwarg names differ from the above, use the installed names and carry them into Tasks 5–6.

- [ ] **Step 4: Record the verification in the pipeline docstring**

Append to the "API surface verified" block in `voicebot/hailhq/voicebot/pipeline.py` (dates and names per Step 3's actual findings):

```python
Speechmatics plugin surface (2026-07-29, multi-language task): verified
``speechmatics.STT`` kwargs (``language``, ``operating_point``,
``api_key``, ``turn_detection_mode``, ``end_of_utterance_silence_trigger``)
and ``TurnDetectionMode`` members against the installed
livekit-plugins-speechmatics.
```

- [ ] **Step 5: Commit**

```bash
git add voicebot/pyproject.toml uv.lock voicebot/hailhq/voicebot/pipeline.py
git commit -m "feat(voicebot): add livekit-plugins-speechmatics dependency"
```

---

### Task 5: Voicebot — `build_stt` speechmatics branch + provider resolution

**Files:**
- Modify: `voicebot/hailhq/voicebot/pipeline.py:344-380` (`build_stt`)
- Test: `voicebot/tests/test_pipeline.py` (extend), `voicebot/tests/test_pipeline_byo.py` (extend)

**Interfaces:**
- Consumes: `resolve_stt_provider`, `SUPPORTED_LANGUAGES` (Task 1); `settings.speechmatics_api_key` (Task 3); verified plugin kwargs (Task 4).
- Produces: `build_stt(org: ResolvedLayer | None = None, language: str | None = None, provider: str = "deepgram", stt_drives_turns: bool = False) -> agents_stt.STT`. Callers (Task 6) resolve `provider` first via `resolve_stt_provider`.

- [ ] **Step 1: Write the failing tests**

Append to `voicebot/tests/test_pipeline.py` (the autouse `_stub_provider_keys` fixture already exists; extend it with the two speechmatics lines shown):

In the `_stub_provider_keys` fixture body add:

```python
    monkeypatch.setenv("SPEECHMATICS_API_KEY", "sm-test-placeholder")
    monkeypatch.setattr(settings, "speechmatics_api_key", "sm-test-placeholder")
```

New tests:

```python
def test_build_stt_speechmatics_house() -> None:
    from livekit.plugins import speechmatics as speechmatics_plugin

    from hailhq.voicebot.pipeline import build_stt

    stt = build_stt(language="da", provider="speechmatics")
    assert isinstance(stt, speechmatics_plugin.STT)


def test_build_stt_deepgram_still_default_shape() -> None:
    from livekit.plugins import deepgram as deepgram_plugin

    from hailhq.voicebot.pipeline import build_stt

    stt = build_stt(language="en", provider="deepgram")
    assert isinstance(stt, deepgram_plugin.STT)


def test_build_stt_speechmatics_without_any_key_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.core.config import settings

    from hailhq.voicebot.pipeline import ProviderKeyError, build_stt

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    with pytest.raises(ProviderKeyError):
        build_stt(language="da", provider="speechmatics")
```

Append to `voicebot/tests/test_pipeline_byo.py` (follow the file's existing `captured_plugins` fixture idiom — read the fixture first and mirror how the deepgram BYO tests build a `ResolvedLayer`):

```python
def test_stt_org_speechmatics_key_used(captured_plugins) -> None:
    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={},
        fallback_enabled=False,
    )
    stt = build_stt(org=org, language="sv", provider="speechmatics")
    assert stt is not None  # constructed with the org key, no exception


def test_stt_org_row_ignored_when_pinned_to_other_provider(
    captured_plugins,
) -> None:
    from livekit.plugins import deepgram as deepgram_plugin

    from hailhq.voicebot.pipeline import ResolvedLayer, build_stt

    org = ResolvedLayer(
        provider="speechmatics",
        api_key="sm-org-key",
        params={},
        fallback_enabled=False,
    )
    # Caller pinned deepgram; the speechmatics org row must not be used.
    stt = build_stt(org=org, language="sv", provider="deepgram")
    assert isinstance(stt, deepgram_plugin.STT)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest voicebot/tests/test_pipeline.py voicebot/tests/test_pipeline_byo.py -v -k "stt"`
Expected: FAIL — `build_stt` has no `provider` parameter yet.

- [ ] **Step 3: Implement `build_stt`**

In `voicebot/hailhq/voicebot/pipeline.py`: add the import alongside the other plugin imports —

```python
from livekit.plugins import (
    speechmatics as speechmatics_plugin,
)
```

Replace `build_stt` with (adjust kwarg names to Task 4's verified surface):

```python
def build_stt(
    org: ResolvedLayer | None = None,
    language: str | None = None,
    provider: str = "deepgram",
    stt_drives_turns: bool = False,
) -> agents_stt.STT:
    """Construct the STT for one call.

    ``provider`` arrives already resolved (per-call pin > org BYO row >
    language auto-route — ``resolve_stt_provider``). The org row is used
    only when its provider matches ``provider``; a row pinned away by the
    per-call choice is ignored rather than billed. ``stt_drives_turns``
    is set when the session's turn detection is ``"stt"`` — Speechmatics
    then runs its ADAPTIVE end-of-utterance mode instead of EXTERNAL.

    Deepgram fallback semantics are unchanged: BYO + fallback_enabled
    appends the house instance. Speechmatics mirrors them.
    """
    org_matches = org is not None and org.provider == provider
    if provider == "speechmatics":
        kwargs: dict[str, Any] = {
            "language": language or "en",
            "operating_point": "enhanced",
        }
        if stt_drives_turns:
            kwargs["turn_detection_mode"] = (
                speechmatics_plugin.TurnDetectionMode.ADAPTIVE
            )
        if org_matches and org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.speechmatics_api_key:
            raise ProviderKeyError("no org or house speechmatics key available")
        byo = speechmatics_plugin.STT(**kwargs)
        if org_matches and org.fallback_enabled and settings.speechmatics_api_key:
            house_kwargs = dict(kwargs)
            house_kwargs.pop("api_key", None)
            return agents_stt.FallbackAdapter(
                [byo, speechmatics_plugin.STT(**house_kwargs)]
            )
        return byo

    house_kwargs: dict[str, Any] = {"model": settings.deepgram_model}
    if language:
        house_kwargs["language"] = language
    if org_matches:
        kwargs = {"model": org.params.get("model") or settings.deepgram_model}
        if language:
            kwargs["language"] = language
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.deepgram_api_key:
            raise ProviderKeyError("no org or house deepgram key available")
        byo = deepgram_plugin.STT(**kwargs)
        if org.fallback_enabled and settings.deepgram_api_key:
            return agents_stt.FallbackAdapter(
                [byo, deepgram_plugin.STT(**house_kwargs)]
            )
        return byo
    return deepgram_plugin.STT(**house_kwargs)
```

Note the behavior change from today: an org STT row whose provider differs from the resolved provider is now *ignored* (house key for the resolved provider) instead of raising — the old `raise ProviderKeyError(f"unknown org stt provider ...")` disappears because `STTParams` now guarantees the row is deepgram or speechmatics. Update `voicebot/tests/test_pipeline_byo.py::test_build_stt_unknown_org_provider_fails_fast` accordingly: construct `ResolvedLayer(provider="whisper", ...)` and assert it is ignored (house deepgram built) rather than raising — or, if the team prefers fail-fast for genuinely unknown strings, keep a final `if org is not None and org.provider not in ("deepgram", "speechmatics"): raise ProviderKeyError(...)` guard at the top. **Choose the guard variant** — it preserves the documented fail-fast tenet:

```python
    if org is not None and org.provider not in ("deepgram", "speechmatics"):
        raise ProviderKeyError(f"unknown org stt provider '{org.provider}'")
```

(Place it as the first statement of `build_stt`; the existing test then passes unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest voicebot/tests/test_pipeline.py voicebot/tests/test_pipeline_byo.py -v`
Expected: all pass, including the pre-existing suite.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix voicebot/hailhq/voicebot/pipeline.py voicebot/tests/test_pipeline.py voicebot/tests/test_pipeline_byo.py
uv run black voicebot/hailhq/voicebot/pipeline.py voicebot/tests/test_pipeline.py voicebot/tests/test_pipeline_byo.py
git add voicebot/
git commit -m "feat(voicebot): speechmatics stt branch with resolved-provider precedence"
```

---

### Task 6: Voicebot — turn-detection wiring in `build_session` + agent pass-through

**Files:**
- Modify: `voicebot/hailhq/voicebot/pipeline.py:383-405` (`build_session`)
- Modify: `voicebot/hailhq/voicebot/agent.py:1106-1143` (voice_cfg parsing + `build_session` call)
- Test: `voicebot/tests/test_pipeline.py` (extend)

**Interfaces:**
- Consumes: `resolve_stt_provider`, `turn_mode_for`, `SUPPORTED_LANGUAGES` (Task 1); `build_stt(org, language, provider, stt_drives_turns)` (Task 5).
- Produces: `build_session(llm_cfg, vad, org_cfgs=None, voice_id_override=None, language=None, stt_choice="auto") -> AgentSession` — sessions now always carry a `turn_detection` argument.

- [ ] **Step 1: Write the failing tests**

Append to `voicebot/tests/test_pipeline.py`:

```python
def _make_session(language, stt_choice="auto"):
    from unittest.mock import MagicMock

    from hailhq.voicebot.pipeline import build_session

    return build_session(
        None, MagicMock(), language=language, stt_choice=stt_choice
    )


def test_session_semantic_turns_for_covered_language() -> None:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel

    session = _make_session("fr")
    assert isinstance(session.turn_detection, MultilingualModel)


def test_session_stt_turns_for_speechmatics_language() -> None:
    from livekit.plugins import speechmatics as speechmatics_plugin

    session = _make_session("da")
    assert session.turn_detection == "stt"
    assert isinstance(session.stt, speechmatics_plugin.STT)


def test_session_vad_turns_when_pinned_away_from_speechmatics() -> None:
    from livekit.plugins import deepgram as deepgram_plugin

    session = _make_session("da", stt_choice="deepgram")
    assert session.turn_detection == "vad"
    assert isinstance(session.stt, deepgram_plugin.STT)


def test_session_auto_falls_back_to_deepgram_without_speechmatics_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenet 4: a deepgram-only self-host still serves 'da' (VAD turns)."""
    from hailhq.core.config import settings
    from livekit.plugins import deepgram as deepgram_plugin

    monkeypatch.setattr(settings, "speechmatics_api_key", "")
    session = _make_session("da")
    assert isinstance(session.stt, deepgram_plugin.STT)
    assert session.turn_detection == "vad"
```

Note: if `AgentSession` does not expose `.turn_detection`/`.stt` as public attributes in the installed livekit-agents, read `agent_session.py` in the venv and assert on the actual attribute names (private `_turn_detection` etc.), following the repo's verified-surface convention.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest voicebot/tests/test_pipeline.py -v -k "session"`
Expected: FAIL — `build_session` has no `stt_choice` parameter.

- [ ] **Step 3: Implement `build_session`**

Add the import at the top of `pipeline.py`:

```python
from hailhq.core.languages import (
    SUPPORTED_LANGUAGES,
    resolve_stt_provider,
    turn_mode_for,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel
```

Replace `build_session`:

```python
def build_session(
    llm_cfg: dict[str, Any] | None,
    vad: agents_vad.VAD,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    voice_id_override: str | None = None,
    language: str | None = None,
    stt_choice: str = "auto",
) -> AgentSession:
    """Build the :class:`AgentSession` for one job.

    ``stt_choice`` is the per-call ``voice_config.stt`` ("auto" routes by
    language). Provider resolution: per-call pin > org BYO row > auto.
    Auto only picks speechmatics when a key exists for it (org or house)
    — a deepgram-only self-host keeps working (tenet 4). Turn detection:
    semantic MultilingualModel for its 14 languages, "stt" when
    speechmatics serves the call, "vad" as the floor.
    """
    org_cfgs = org_cfgs or {}
    org_stt = org_cfgs.get("stt")
    provider = resolve_stt_provider(
        stt_choice, org_stt.provider if org_stt else None, language
    )
    if language is not None and provider not in SUPPORTED_LANGUAGES[language].stt:
        # Direct dispatch can bypass the API's 422 gate; deepgram covers
        # every supported language, so degrade rather than fail the call.
        provider = "deepgram"
    if (
        provider == "speechmatics"
        and stt_choice == "auto"
        and not settings.speechmatics_api_key
        and not (org_stt and org_stt.provider == "speechmatics" and org_stt.api_key)
    ):
        provider = "deepgram"
    mode = turn_mode_for(language, provider)
    turn_detection: Any = MultilingualModel() if mode == "semantic" else mode
    return AgentSession(
        vad=vad,
        stt=build_stt(
            org_stt, language, provider, stt_drives_turns=(mode == "stt")
        ),
        tts=build_tts(org_cfgs.get("tts"), voice_id_override, language),
        llm=build_llm(llm_cfg, org_cfgs.get("llm")),
        turn_detection=turn_detection,
    )
```

- [ ] **Step 4: Pass the per-call choice through from agent.py**

In `voicebot/hailhq/voicebot/agent.py` (around line 1108), after `language = voice_cfg.get("language")` add:

```python
    stt_choice = voice_cfg.get("stt") or "auto"
```

and extend the `build_session(...)` call (around line 1137):

```python
        session = build_session(
            llm_cfg,
            vad,
            org_cfgs=org_cfgs,
            voice_id_override=voice_id_override,
            language=language,
            stt_choice=stt_choice,
        )
```

- [ ] **Step 5: Run the full voicebot suite**

Run: `uv run pytest voicebot/tests -q`
Expected: all pass. `test_agent*.py` builds sessions through the same path — if any fixture feeds a language outside the matrix, fix the fixture (the matrix is now authoritative).

- [ ] **Step 6: Verify turn-detector model downloads (Docker parity)**

Run: `cd voicebot && uv run python -m hailhq.voicebot.main download-files && cd ..`
Expected: exits 0; downloads MultilingualModel weights (the Dockerfile already runs this — no Dockerfile change needed).

- [ ] **Step 7: Lint and commit**

```bash
uv run ruff check --fix voicebot/hailhq/voicebot/pipeline.py voicebot/hailhq/voicebot/agent.py voicebot/tests/test_pipeline.py
uv run black voicebot/hailhq/voicebot/pipeline.py voicebot/hailhq/voicebot/agent.py voicebot/tests/test_pipeline.py
git add voicebot/
git commit -m "feat(voicebot): per-language turn detection (semantic/stt/vad) and stt auto-routing"
```

---

### Task 7: Voicebot — trim ElevenLabs from house TTS for Cartesia-only languages

**Files:**
- Modify: `voicebot/hailhq/voicebot/pipeline.py:253-273` (`_house_tts`)
- Test: `voicebot/tests/test_pipeline.py` (extend)

**Interfaces:**
- Consumes: `tts_providers_for` (Task 1).
- Produces: unchanged signature `_house_tts(voice_id_override, language) -> list[agents_tts.TTS]`; the list simply omits providers that can't speak the language.

- [ ] **Step 1: Write the failing tests**

```python
def test_house_tts_trims_elevenlabs_for_cartesia_only_language() -> None:
    from livekit.plugins import cartesia as cartesia_plugin

    from hailhq.voicebot.pipeline import build_tts

    tts = build_tts(language="th")  # th: no elevenlabs support
    assert isinstance(tts, cartesia_plugin.TTS)  # single instance, no adapter


def test_house_tts_keeps_fallback_for_dual_provider_language() -> None:
    from livekit.agents import tts as agents_tts

    from hailhq.voicebot.pipeline import build_tts

    tts = build_tts(language="da")
    assert isinstance(tts, agents_tts.FallbackAdapter)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest voicebot/tests/test_pipeline.py -v -k "house_tts"`
Expected: FAIL — `th` currently returns a FallbackAdapter with both providers.

- [ ] **Step 3: Implement the trim**

In `_house_tts`, add the import usage (already imported in Task 6) and gate each provider:

```python
def _house_tts(
    voice_id_override: str | None, language: str | None
) -> list[agents_tts.TTS]:
    allowed = tts_providers_for(language)
    instances: list[agents_tts.TTS] = []
    if settings.cartesia_api_key and "cartesia" in allowed:
        kwargs: dict[str, Any] = {
            "model": settings.cartesia_model,
            "voice": voice_id_override or settings.cartesia_voice_id,
        }
        if language:
            kwargs["language"] = language
        instances.append(cartesia_plugin.TTS(**kwargs))
    if settings.eleven_api_key and "elevenlabs" in allowed:
        kwargs = {
            "voice_id": voice_id_override or settings.elevenlabs_voice_id,
            "model": settings.elevenlabs_model,
        }
        if language:
            kwargs["language"] = language
        instances.append(elevenlabs_plugin.TTS(**kwargs))
    return instances
```

Extend the `from hailhq.core.languages import (...)` line from Task 6 with `tts_providers_for`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest voicebot/tests -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix voicebot/hailhq/voicebot/pipeline.py voicebot/tests/test_pipeline.py
uv run black voicebot/hailhq/voicebot/pipeline.py voicebot/tests/test_pipeline.py
git add voicebot/
git commit -m "feat(voicebot): drop elevenlabs tts fallback for languages it cannot speak"
```

---

### Task 8: API — request-time 422 for incompatible language/provider combos

**Files:**
- Modify: `api/hailhq/api/routes/calls.py` (insert the gate after the tool-allowlist gate, ~line 220, before the compliance gate)
- Test: `api/tests/test_calls_api.py` (extend)

**Interfaces:**
- Consumes: `SUPPORTED_LANGUAGES`, `resolve_stt_provider` (Task 1); `load_org_provider_configs` (existing, `hailhq.core.provider_config`); `unprocessable` (existing, `api/hailhq/api/errors.py`); `cache_failure` idiom (existing in this route).
- Produces: POST /calls rejects with 422 + field `loc` when (a) a pinned `stt` provider doesn't support the language, or (b) the org's BYO TTS provider doesn't support it.

- [ ] **Step 1: Write the failing tests**

Append to `api/tests/test_calls_api.py`, mirroring `test_per_call_voice_id_rides_in_voice_config` (line 904) — same fixtures (`client: httpx.AsyncClient`, `async_session: AsyncSession`, `org_and_key: tuple[str, ApiKey, str]`, `livekit_mock: AsyncMock`, `add_phone_number`), same auth-header shape:

```python
async def test_pinned_stt_incompatible_with_language_422(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "voice_config": {"stt": "speechmatics", "language": "gu"},
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422, resp.text
    assert "speechmatics" in resp.text
    livekit_mock.dispatch_agent.assert_not_awaited()


async def test_unsupported_language_code_422(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "voice_config": {"language": "ka"},  # excluded from the matrix
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422, resp.text  # pydantic enum rejection


async def test_supported_language_with_auto_stt_accepted(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "voice_config": {"language": "da"},
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    metadata = livekit_mock.dispatch_agent.await_args.kwargs["metadata"]
    assert metadata["voice_config"]["language"] == "da"


async def test_byo_elevenlabs_tts_with_cartesia_only_language_422(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
    async_session.add(
        OrgProviderConfig(
            organization_id=org_id,
            layer="tts",
            provider="elevenlabs",
            params={"provider": "elevenlabs"},
            is_active=True,
        )
    )
    await async_session.commit()

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
            "voice_config": {"language": "th"},  # cartesia-only language
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422, resp.text
    assert "elevenlabs" in resp.text
    livekit_mock.dispatch_agent.assert_not_awaited()
```

Import `OrgProviderConfig` from `hailhq.core.models` at the top of the test file if not already imported. Check the model's actual column set (`grep -n "class OrgProviderConfig" -A 15 core/hailhq/core/models.py`) and adjust the seeding kwargs to the real columns (e.g. whether `params` carries the provider, or a dedicated `provider` column exists) — grep `OrgProviderConfig(` under `api/tests/` first; if another test already seeds one, copy that construction verbatim.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `uv run pytest api/tests/test_calls_api.py -v -k "language or pinned_stt or byo_elevenlabs"`
Expected: the pinned-stt and byo tests FAIL (route accepts them today); the enum test may already pass after Task 2 — that's fine.

- [ ] **Step 3: Implement the gate**

In `api/hailhq/api/routes/calls.py`, add imports:

```python
from hailhq.core.languages import SUPPORTED_LANGUAGES, resolve_stt_provider
from hailhq.core.provider_config import load_org_provider_configs
```

Insert after the tool-allowlist gate and before the compliance gate:

```python
    # Language/provider compatibility gate — reject before any Call row is
    # created. Deterministic on request + org config, so failures are
    # cached for idempotent replay like the other 422 gates.
    lang = body.voice_config.language
    if lang is not None:
        caps = SUPPORTED_LANGUAGES[lang]
        org_rows = await load_org_provider_configs(
            db, principal.organization_id
        )
        stt_row = org_rows.get("stt")
        provider = resolve_stt_provider(
            body.voice_config.stt,
            stt_row.provider if stt_row is not None else None,
            lang,
        )
        if body.voice_config.stt != "auto" and provider not in caps.stt:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"stt provider '{provider}' does not support language "
                    f"'{lang}'; supported providers: {sorted(caps.stt)}",
                    loc=["body", "voice_config", "stt"],
                ),
            )
        tts_row = org_rows.get("tts")
        if tts_row is not None and tts_row.provider not in caps.tts:
            raise await cache_failure(
                idem,
                unprocessable(
                    f"your BYO tts provider '{tts_row.provider}' does not "
                    f"support language '{lang}'; supported providers: "
                    f"{sorted(caps.tts)}",
                    loc=["body", "voice_config", "language"],
                ),
            )
```

Note: `load_org_provider_configs` returns raw `OrgProviderConfig` rows — `row.provider` is the column; no decryption happens here (keys aren't needed for the capability check).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest api/tests/test_calls_api.py -q`
Expected: all pass (pre-existing suite included).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix api/hailhq/api/routes/calls.py api/tests/test_calls_api.py
uv run black api/hailhq/api/routes/calls.py api/tests/test_calls_api.py
git add api/
git commit -m "feat(api): 422 language/provider compatibility gate on call creation"
```

---

### Task 9: Speechmatics key probe in provider validation

**Files:**
- Modify: `core/hailhq/core/provider_validation.py:116-124` (add branch next to the deepgram probe)
- Test: `core/tests/test_provider_validation.py` (extend the `PROBES` table)

**Interfaces:**
- Consumes: `_Probe`, `_classify` (existing module internals).
- Produces: `validate_provider_key("stt", "speechmatics", key, {})` returns `("valid", "ok")` for a working key.

- [ ] **Step 1: Write the failing test**

`core/tests/test_provider_validation.py` is table-driven: `test_probe_shape` and `test_401_is_invalid` parametrize over the `PROBES` list (lines 10-62). Add one tuple to `PROBES` after the cartesia entry:

```python
    (
        "speechmatics",
        "stt",
        {},
        "GET",
        "https://asr.api.speechmatics.com/v2/jobs",
        "authorization",
        "Bearer ",
        "auth",
    ),
```

That single entry gives the 200→valid, 401→invalid, and header-shape assertions for free via the existing parametrized tests.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest core/tests -q -k speechmatics`
Expected: FAIL — `validate_provider_key` returns `("invalid", "unknown provider 'speechmatics' for layer 'stt'")`.

- [ ] **Step 3: Implement the probe**

In `_probe_for`, after the deepgram branch:

```python
    if provider == "speechmatics":
        # Auth probe: the batch jobs listing is a cheap authenticated GET;
        # 200 proves the key, 401/403 disproves it.
        return _Probe(
            "GET",
            "https://asr.api.speechmatics.com/v2/jobs",
            {"Authorization": f"Bearer {api_key}"},
            None,
            "auth",
        )
```

If `_Probe.json_body` is typed as `dict` (not `dict | None`), widen the annotation to `dict | None` — `httpx` sends no body for `json=None`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest core/tests -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix core/hailhq/core/provider_validation.py core/tests/
uv run black core/hailhq/core/provider_validation.py core/tests/
git add core/
git commit -m "feat(core): speechmatics key probe for BYO validation"
```

---

### Task 10: OpenAPI regen + CLI client regen + `--stt` flag

**Files:**
- Regenerate: `openapi/openapi.yaml`, `cli/internal/client/client.gen.go`
- Modify: `cli/internal/cmd/call.go:25,77,107-108`
- Test: `cli/internal/cmd/call_test.go` (extend, following its existing flag tests)

**Interfaces:**
- Consumes: the Task 2 schema (language enum + stt selector) via the regenerated spec.
- Produces: `hail call --language da --stt speechmatics` sends `voice_config: {"language": "da", "stt": "speechmatics"}`.

- [ ] **Step 1: Regenerate the OpenAPI spec from the live app**

```bash
cd api && (uv run uvicorn hailhq.api.main:app --port 8080 &) && sleep 3 && cd ..
curl -s http://localhost:8080/openapi.json \
  | python3 -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
kill %1 2>/dev/null || pkill -f "uvicorn hailhq.api.main:app" || true
```

Verify: `grep -A8 "language:" openapi/openapi.yaml | head -30` shows the 39-value enum, and the `stt` property shows `auto`/`deepgram`/`speechmatics`. (If uvicorn needs env/DB to boot, source `.env.local` first the way the dev-commands section of CLAUDE.md runs it.)

- [ ] **Step 2: Regenerate the Go client**

Run: `cd cli && make codegen && cd ..`
Expected: `client.gen.go` regenerates; `VoiceConfig` now has enum-typed `Language` and `Stt` fields (oapi-codegen names like `VoiceConfigLanguage`/`VoiceConfigStt` — check the generated file for the exact type names).

- [ ] **Step 3: Write the failing CLI test**

Extend `cli/internal/cmd/call_test.go` following its existing language-flag test pattern (read the file first; mirror the assertion style):

```go
func TestCallSttFlagRidesInVoiceConfig(t *testing.T) {
	// mirror the existing --language flag test: build the command with
	// --language da --stt speechmatics, capture the request body, assert
	// voice_config.stt == "speechmatics" and voice_config.language == "da".
}
```

Fill the body by copying the existing `--language` test and adding the flag + assertion — same server-stub, same helpers.

- [ ] **Step 4: Run to verify failure**

Run: `cd cli && go test ./internal/cmd/ -run TestCallStt -v && cd ..`
Expected: FAIL — flag doesn't exist.

- [ ] **Step 5: Implement the flag**

In `cli/internal/cmd/call.go`: add to the flags struct (line ~25) `stt string`; register (line ~77):

```go
	cmd.Flags().StringVar(&f.stt, "stt", "", "STT provider: deepgram or speechmatics; default auto-routes by language")
```

Replace the voice-config build (lines ~107-108) so both flags compose (adjust the generated enum type names to Step 2's actual output):

```go
	if f.language != "" || f.stt != "" {
		vc := &client.VoiceConfig{}
		if f.language != "" {
			lang := client.VoiceConfigLanguage(f.language)
			vc.Language = &lang
		}
		if f.stt != "" {
			stt := client.VoiceConfigStt(f.stt)
			vc.Stt = &stt
		}
		body.VoiceConfig = vc
	}
```

Also update the `--language` flag help text to mention the enum: `"Spoken language for the call, lowercase ISO 639-1 (e.g. da); one of the 39 supported codes, see docs/languages.md; default English"`.

- [ ] **Step 6: Run CLI tests + vet**

Run: `cd cli && go test ./... && go vet ./... && gofmt -w . && cd ..`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add openapi/openapi.yaml cli/
git commit -m "feat(cli): regen client for language enum, add --stt flag"
```

---

### Task 11: SDK — language enum + stt parameter

**Files:**
- Modify: `sdk/hail/models.py:51-60` (`VoiceConfig`)
- Modify: `sdk/hail/client.py:82-123` (`create` signature + body build)
- Test: `sdk/tests/test_client.py` (extend, mirroring its existing language test)

**Interfaces:**
- Consumes: nothing from other tasks (the SDK is standalone; it mirrors the API contract by hand).
- Produces: `hail.CallsResource.create(..., language="da", stt="speechmatics")` sends `voice_config: {"language": "da", "stt": "speechmatics"}`.

- [ ] **Step 1: Write the failing test**

Add beside `test_calls_create_language_lands_in_voice_config` (sdk/tests/test_client.py:105), reusing its respx idiom and the file's existing `make_call_response` helper and fixtures:

```python
@respx.mock
async def test_calls_create_stt_lands_in_voice_config(
    base_url: str, api_key: str
) -> None:
    """stt= composes with language= under voice_config; omitted -> absent."""
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            recipient_consent=True,
            language="da",
            stt="speechmatics",
        )
        await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            recipient_consent=True,
            stt="deepgram",
        )
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[0]["voice_config"] == {"language": "da", "stt": "speechmatics"}
    assert bodies[1]["voice_config"] == {"stt": "deepgram"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest sdk/tests/test_client.py -q -k stt`
Expected: FAIL — `create` has no `stt` parameter.

- [ ] **Step 3: Implement**

`sdk/hail/models.py` — `VoiceConfig` becomes (module defines models by hand; keep the file's comment style):

```python
class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stt: Literal["auto", "deepgram", "speechmatics"] = "auto"
    tts: Literal["cartesia"] = "cartesia"
    vad: Literal["silero"] = "silero"
    turn_detection: Literal["livekit"] = "livekit"
    # Spoken language for the call (lowercase ISO 639-1, e.g. "da").
    # One of the 39 supported codes (see docs/languages.md);
    # None -> provider defaults (English).
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
```

(The SDK keeps the loose pattern rather than duplicating the 39-code enum — the server is authoritative and the SDK philosophy in `create()`'s docstring is "we don't pre-validate so SDK and API stay in lockstep".)

`sdk/hail/client.py` — add to the `create` signature after `language`:

```python
        stt: Literal["auto", "deepgram", "speechmatics"] | None = None,
```

extend the docstring sentence about `language` with:

```
        ``stt`` pins the speech-to-text provider ("deepgram" or
        "speechmatics"); omit for automatic per-language routing.
```

and replace the body-build lines:

```python
        voice_config: dict[str, Any] = {}
        if language is not None:
            voice_config["language"] = language
        if stt is not None:
            voice_config["stt"] = stt
        if voice_config:
            body["voice_config"] = voice_config
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest sdk/tests -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix sdk/hail/models.py sdk/hail/client.py sdk/tests/test_client.py
uv run black sdk/hail/models.py sdk/hail/client.py sdk/tests/test_client.py
git add sdk/
git commit -m "feat(sdk): stt provider parameter on calls.create"
```

---

### Task 12: MCP — `stt` on place_call + language description update

**Files:**
- Modify: `mcp/hailhq/mcp/tools.py:530-615` (`place_call_tool`)
- Modify: `mcp/hailhq/mcp/hail_client.py:100-140` (`place_call`)
- Test: `mcp/tests/test_tools.py` (extend, mirroring its existing language test)

**Interfaces:**
- Consumes: the API contract from Task 8 (server enforces validity; MCP passes through).
- Produces: `place_call(..., language="da", stt="speechmatics")` MCP tool call sends `voice_config: {"language": "da", "stt": "speechmatics"}`.

- [ ] **Step 1: Write the failing test**

Add beside `test_place_call_language_lands_in_voice_config` (mcp/tests/test_tools.py:99), reusing its `_handler`-capture idiom and the file's `_call_response` helper and `client` fixture:

```python
@respx.mock
async def test_place_call_stt_lands_in_voice_config(client: HailClient) -> None:
    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.read().decode("utf-8")
        return httpx.Response(201, json=_call_response())

    respx.post(f"{_BASE_URL}/calls").mock(side_effect=_handler)

    result = await tools.place_call(
        client=client,
        recipient_consent=True,
        to="+14155559999",
        system_prompt="be polite",
        language="da",
        stt="speechmatics",
    )
    assert "error" not in result, result

    body = httpx.Response(200, content=captured["body"]).json()
    assert body["voice_config"] == {"language": "da", "stt": "speechmatics"}
```

Note: `tools.place_call` here is the client-layer helper this test file imports (`mcp/hailhq/mcp/hail_client.py`'s `place_call` re-exported) — match the import the sibling test uses. Also extend `test_place_call_rejects_bad_language` (line 144) if it asserts on the old free-form pattern message.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest mcp/tests/test_tools.py -q -k stt`
Expected: FAIL — no `stt` parameter.

- [ ] **Step 3: Implement**

`mcp/hailhq/mcp/hail_client.py::place_call`: add parameter `stt: str | None = None` after `language`, and where the body assembles `voice_config` from `language`, build it from both (same shape as the SDK change in Task 11).

`mcp/hailhq/mcp/tools.py::place_call_tool`: add `stt: str | None = None` after `language`; pass `stt=stt` through to `place_call`; replace the `language` docstring sentence with:

```
        ``language`` sets the call's spoken language for speech-to-text,
        text-to-speech, and turn detection, as a lowercase ISO 639-1 code
        (e.g. ``"da"``); 39 languages are supported (server rejects others
        with 422); omit for English. ``stt`` pins the speech-to-text
        provider (``"deepgram"`` or ``"speechmatics"``); omit for
        automatic per-language routing.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest mcp/tests -q`
Expected: all pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check --fix mcp/hailhq/mcp/tools.py mcp/hailhq/mcp/hail_client.py mcp/tests/test_tools.py
uv run black mcp/hailhq/mcp/tools.py mcp/hailhq/mcp/hail_client.py mcp/tests/test_tools.py
git add mcp/
git commit -m "feat(mcp): stt provider parameter on place_call"
```

---

### Task 13: Docs — languages page, README, setup

**Files:**
- Create: `docs/languages.md`
- Modify: `README.md` (add a Languages section near the existing feature list)
- Modify: `docs/cli.md` (the `--language` example) and whichever `docs/setup/*` file documents provider API keys (grep `DEEPGRAM_API_KEY` under `docs/`).

**Interfaces:** none — prose only. Follow the agent-first docs tenet: runnable example first, canonical links, one screen per page.

- [ ] **Step 1: Write `docs/languages.md`**

```markdown
# Languages

Place a call in any of 39 languages by setting `voice_config.language`
(lowercase ISO 639-1):

​```bash
hail call +4512345678 --language da \
  --system-prompt "Book a table for two tomorrow at 19:00." \
  --recipient-consent
​```

Hail picks the speech-to-text provider and turn-detection strategy per
language automatically. Pin the STT provider with `--stt deepgram|speechmatics`
(API: `voice_config.stt`; default `auto`). Unsupported codes and
incompatible language/provider combos are rejected with `422`.

| Code | Language | Auto STT | Turn detection | TTS fallback |
|---|---|---|---|---|
| ar | Arabic | speechmatics | STT-adaptive | yes |
| bg | Bulgarian | speechmatics | STT-adaptive | yes |
| bn | Bengali | speechmatics | STT-adaptive | no |
| cs | Czech | speechmatics | STT-adaptive | yes |
| da | Danish | speechmatics | STT-adaptive | yes |
| de | German | deepgram | semantic | yes |
| el | Greek | speechmatics | STT-adaptive | yes |
| en | English | deepgram | semantic | yes |
| es | Spanish | deepgram | semantic | yes |
| fi | Finnish | speechmatics | STT-adaptive | yes |
| fr | French | deepgram | semantic | yes |
| gu | Gujarati | deepgram | VAD | no |
| he | Hebrew | speechmatics | STT-adaptive | no |
| hi | Hindi | deepgram | semantic | yes |
| hr | Croatian | speechmatics | STT-adaptive | yes |
| hu | Hungarian | speechmatics | STT-adaptive | yes |
| id | Indonesian | deepgram | semantic | yes |
| it | Italian | deepgram | semantic | yes |
| ja | Japanese | deepgram | semantic | yes |
| kn | Kannada | deepgram | VAD | no |
| ko | Korean | deepgram | semantic | yes |
| mr | Marathi | speechmatics | STT-adaptive | no |
| ms | Malay | speechmatics | STT-adaptive | yes |
| nl | Dutch | deepgram | semantic | yes |
| no | Norwegian | speechmatics | STT-adaptive | yes |
| pl | Polish | speechmatics | STT-adaptive | yes |
| pt | Portuguese | deepgram | semantic | yes |
| ro | Romanian | speechmatics | STT-adaptive | yes |
| ru | Russian | deepgram | semantic | yes |
| sk | Slovak | speechmatics | STT-adaptive | yes |
| sv | Swedish | speechmatics | STT-adaptive | yes |
| ta | Tamil | speechmatics | STT-adaptive | yes |
| te | Telugu | deepgram | VAD | no |
| th | Thai | speechmatics | STT-adaptive | no |
| tl | Tagalog | speechmatics | STT-adaptive | yes |
| tr | Turkish | deepgram | semantic | yes |
| uk | Ukrainian | speechmatics | STT-adaptive | yes |
| vi | Vietnamese | speechmatics | STT-adaptive | yes |
| zh | Chinese | deepgram | semantic | yes |

Column meanings:

- **Auto STT** — provider chosen when `stt` is `auto`. "speechmatics"
  requires `SPEECHMATICS_API_KEY` (or a BYO Speechmatics key); without
  one, the call falls back to Deepgram with VAD turn detection.
- **Turn detection** — `semantic` = LiveKit's transcript-based turn
  model; `STT-adaptive` = Speechmatics' built-in end-of-utterance
  detection; `VAD` = silence-gap only.
- **TTS fallback** — "no" means only Cartesia speaks this language, so
  no ElevenLabs failover is attached (and a BYO ElevenLabs TTS config is
  rejected for it).

Canonical data: [`core/hailhq/core/languages.py`](../core/hailhq/core/languages.py)
(provider doc sources in its docstring). Voice routing:
[`voicebot/hailhq/voicebot/pipeline.py`](../voicebot/hailhq/voicebot/pipeline.py).
```

(Remove the zero-width characters around the inner code fence when writing the real file — shown here only to nest the fences.)

- [ ] **Step 2: README + setup + cli docs**

- `README.md`: add a one-paragraph "Languages" bullet/section: "39 call languages with automatic STT routing and per-language turn detection — see [docs/languages.md](docs/languages.md)." Place it wherever the README lists voice capabilities (read it first; match its tone).
- `docs/cli.md`: update the `--language` mention to reference the 39 supported codes + `--stt`, linking `docs/languages.md`.
- The setup doc that lists provider keys: add `SPEECHMATICS_API_KEY` next to `DEEPGRAM_API_KEY` with the same one-line style, noting it's optional (Deepgram-only self-hosts keep working).

- [ ] **Step 3: Verify docs render and links resolve**

Run: `grep -n "languages.md" README.md docs/cli.md` — both link. Eyeball the table in a Markdown preview (39 rows).

- [ ] **Step 4: Commit**

```bash
git add docs/languages.md README.md docs/cli.md docs/setup/
git commit -m "docs: language support matrix and speechmatics setup"
```

---

### Task 14: Full verification sweep

- [ ] **Step 1: Full Python test suite**

Run: `uv run pytest core api voicebot sdk mcp -q`
Expected: all pass.

- [ ] **Step 2: Lint everything changed**

Run: `uv run ruff check core api voicebot sdk mcp && uv run black --check core api voicebot sdk mcp`
Expected: clean.

- [ ] **Step 3: Go build + tests**

Run: `cd cli && go build ./... && go test ./... && go vet ./... && cd ..`
Expected: clean.

- [ ] **Step 4: OpenAPI freshness check (mirrors CI)**

Re-run the Task 10 Step 1 dump and `git diff --stat openapi/openapi.yaml` — no diff expected.

- [ ] **Step 5: Report**

Report to the user: test totals, the deploy TODO (`SPEECHMATICS_API_KEY` on the prod VM `.env`), and that PR 2 (hail-website console Speechmatics BYO option) is next with its own plan. Hand over the push command (`git push -u origin feat/multi-language-voicebot`) — do not push or open a PR (user's rule: never `gh pr create`).
