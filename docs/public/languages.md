# Languages

Your agent talks in any of 39 languages. Set `voice_config.language` to a
lowercase ISO 639-1 code, on a call or on an [agent](agents.md):

```bash
hail call +14155550100 --language es \
  --prompt "Confirm the delivery window for order A1001." \
  --recipient-consent
```

The same field works on `POST /v1/calls`, on agents, and in the MCP
`place_call` tool. Omitted, the call is in English. Hail picks the
speech-to-text provider and turn detection for each language. The agent's
built-in spoken lines, such as the AI line, are said in the call's language.

An unsupported code, or a language your own text-to-speech provider cannot
speak, is refused with `422`.

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

What the columns mean:

- **Auto STT**: the speech-to-text provider Hail picks for the language. A
  speech-to-text provider you save in the console always wins. On a
  self-hosted install, `speechmatics` needs `SPEECHMATICS_API_KEY`; without
  it, the call uses Deepgram with VAD turn detection.
- **Turn detection**: how the agent knows the other person finished
  speaking. `semantic` is LiveKit's audio turn detector. `STT-adaptive` is
  Speechmatics' end-of-speech detection. `VAD` waits for a gap of silence.
- **TTS fallback**: `no` means only Cartesia speaks this language, so there
  is no ElevenLabs backup voice. A saved ElevenLabs voice is refused for it.

Canonical data: [`core/hailhq/core/languages.py`](../../core/hailhq/core/languages.py).
Voice routing: [`voicebot/hailhq/voicebot/pipeline.py`](../../voicebot/hailhq/voicebot/pipeline.py).
More call settings: [Call controls](call-controls.md).
