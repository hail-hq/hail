# Languages

Place a call in any of 39 languages by setting `voice_config.language`
(lowercase ISO 639-1):

```bash
hail call +4512345678 --language da \
  --prompt "Book a table for two tomorrow at 19:00." \
  --recipient-consent
```

Hail picks the speech-to-text provider and turn-detection strategy per
language automatically. Pin the STT provider with `--stt deepgram|speechmatics`
(API: `voice_config.stt`; default `auto`). Unsupported codes and
incompatible language/provider combos are rejected with `422`.

| Code | Language   | Auto STT     | Turn detection | TTS fallback |
| ---- | ---------- | ------------ | -------------- | ------------ |
| ar   | Arabic     | speechmatics | STT-adaptive   | yes          |
| bg   | Bulgarian  | speechmatics | STT-adaptive   | yes          |
| bn   | Bengali    | speechmatics | STT-adaptive   | no           |
| cs   | Czech      | speechmatics | STT-adaptive   | yes          |
| da   | Danish     | speechmatics | STT-adaptive   | yes          |
| de   | German     | deepgram     | semantic       | yes          |
| el   | Greek      | speechmatics | STT-adaptive   | yes          |
| en   | English    | deepgram     | semantic       | yes          |
| es   | Spanish    | deepgram     | semantic       | yes          |
| fi   | Finnish    | speechmatics | STT-adaptive   | yes          |
| fr   | French     | deepgram     | semantic       | yes          |
| gu   | Gujarati   | deepgram     | VAD            | no           |
| he   | Hebrew     | speechmatics | STT-adaptive   | no           |
| hi   | Hindi      | deepgram     | semantic       | yes          |
| hr   | Croatian   | speechmatics | STT-adaptive   | yes          |
| hu   | Hungarian  | speechmatics | STT-adaptive   | yes          |
| id   | Indonesian | deepgram     | semantic       | yes          |
| it   | Italian    | deepgram     | semantic       | yes          |
| ja   | Japanese   | deepgram     | semantic       | yes          |
| kn   | Kannada    | deepgram     | VAD            | no           |
| ko   | Korean     | deepgram     | semantic       | yes          |
| mr   | Marathi    | speechmatics | STT-adaptive   | no           |
| ms   | Malay      | speechmatics | STT-adaptive   | yes          |
| nl   | Dutch      | deepgram     | semantic       | yes          |
| no   | Norwegian  | speechmatics | STT-adaptive   | yes          |
| pl   | Polish     | speechmatics | STT-adaptive   | yes          |
| pt   | Portuguese | deepgram     | semantic       | yes          |
| ro   | Romanian   | speechmatics | STT-adaptive   | yes          |
| ru   | Russian    | deepgram     | semantic       | yes          |
| sk   | Slovak     | speechmatics | STT-adaptive   | yes          |
| sv   | Swedish    | speechmatics | STT-adaptive   | yes          |
| ta   | Tamil      | speechmatics | STT-adaptive   | yes          |
| te   | Telugu     | deepgram     | VAD            | no           |
| th   | Thai       | speechmatics | STT-adaptive   | no           |
| tl   | Tagalog    | speechmatics | STT-adaptive   | yes          |
| tr   | Turkish    | deepgram     | semantic       | yes          |
| uk   | Ukrainian  | speechmatics | STT-adaptive   | yes          |
| vi   | Vietnamese | speechmatics | STT-adaptive   | yes          |
| zh   | Chinese    | deepgram     | semantic       | yes          |

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
