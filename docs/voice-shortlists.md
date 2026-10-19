# Voice choices in onboarding and Call settings

The backend owns the language options and voice selection. The website displays
at most three choices and the existing Advanced provider controls. It does not
keep a separate onboarding language or voice allowlist.

## Language and model eligibility

`core/hailhq/core/languages.py` declares Hail's pipeline capabilities. Its 39
languages predate onboarding. Coverage requires both speech recognition and
speech synthesis. Provider catalogue growth does not automatically extend the
pipeline or its public API contract.

Cartesia does not provide a model-capability discovery endpoint. The registry
therefore records reviewed Sonic 3.5 and 3.6 model IDs and their intersection
with Hail's pipeline languages. Unknown models get no automatic suggestions.
Operators can declare another model's capabilities through
`CARTESIA_MODEL_LANGUAGES`. Advanced provider editing can still use other model
IDs. Review the model documentation before adding a capability override.

ElevenLabs capabilities come from `GET /v1/models`. A voice must also list the
requested language and the configured model in `verified_languages`.

## Discovery and selection

Both integrations walk every catalogue page before selection. A missing,
repeated or excessive pagination cursor fails that provider's discovery instead
of silently ranking an incomplete page. The catalogue cache lasts 15 minutes
and includes provider, model and a credential fingerprint in its key.

Cartesia discovery includes active public voices. A requested language must
match a supported accent, or the reported primary language for legacy entries
without accents. Additional multilingual accents count as support.
`is_native` identifies the original accent. It does not describe the complete
set of languages a voice can speak.

ElevenLabs discovery includes default premade voices. Private house-account
clones and saved community voices are excluded. A recommendation should remain
accessible when a workspace retains its own provider key.

Selection applies these rules in order:

1. Keep eligible operator preferences in their configured order.
2. Include eligible voices from Hail's existing default voice configuration.
3. Fill remaining places with original-language voices first. Prefer a gender,
   provider and accent region not already represented. Break ties by name,
   provider and voice ID for stable results.

These are listening suggestions. The algorithm does not assign quality scores
or infer quality from description keywords. Human listening review is still
needed to call a voice preferred for a language or use case.

Accent flags use the provider's locale for the requested language. Ambiguous
accent metadata uses a neutral icon. Country of origin is never an accent.

One failed provider does not remove choices from another provider. The response
marks partial discovery so the website can offer Retry. All pipeline language
options remain available, including those without house suggestions.

## Optional editorial preferences

`VOICE_SHORTLIST_PREFERENCES` is JSON configuration. No voice IDs are embedded
in the selection algorithm. An example configuration shape is:

```json
{
  "fr": [
    {
      "provider": "cartesia",
      "voice_id": "reviewed-voice-id",
      "reason": "French listening review: pronunciation and conversational pacing."
    }
  ]
}
```

The example ID is a placeholder. Default configuration is empty. Each entry
requires a reason for future review. Unavailable or incompatible preferences
are ignored. Configuration changes follow the normal service configuration
process; this PR does not change production configuration.

## Listening and saving

Samples use fixed translated greetings. Discovery does not synthesize audio.
The first listen requests the chosen provider and configured model. Both
providers return a WAV sample to the website. The sample cache includes
provider, model, language and voice ID. The browser URL also includes the model.
No customer text or key reaches the browser.

Picker saves validate language support with the actual key, model and voice
that will run. This includes retaining a custom voice while changing language.
A failed check leaves saved settings intact. Advanced provider editing remains
available when an operator needs a model or voice outside the suggestion policy.
Selecting a suggestion stores its validated provider and model while retaining
that provider's stored key and fallback choice.

## Provider references

Reviewed on 2026-10-19:

- [Cartesia voice catalogue](https://docs.cartesia.ai/api-reference/voices/list)
- [Cartesia multilingual voices](https://docs.cartesia.ai/build-with-cartesia/capability-guides/multilingual-voices)
- [Cartesia Sonic 3.5](https://docs.cartesia.ai/build-with-cartesia/tts-models/sonic-3-5)
- [Cartesia Sonic 3.6](https://docs.cartesia.ai/build-with-cartesia/tts-models/latest)
- [ElevenLabs voice catalogue](https://elevenlabs.io/docs/api-reference/voices/search)
- [ElevenLabs model capabilities](https://elevenlabs.io/docs/api-reference/models/list)
