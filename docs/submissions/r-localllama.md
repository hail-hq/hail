---
target: "r/LocalLLaMA"
slug: r-localllama
category: subreddit
url: "https://www.reddit.com/r/LocalLLaMA/"
score: 2
status: drafted
---

# r/LocalLLaMA

## TODO

- [ ] **BLOCKED — do not submit.** r/LocalLLaMA's culture and rules select for genuinely local, open-weight inference (self-hosted models, quantization/hardware specifics, tokens/sec benchmarks). Hail's current stack does not qualify — see Notes.
- [ ] Build a local/open-weight variant of the voice pipeline: swap the hosted LLM fallback chain (OpenAI → Google → Anthropic, see `voicebot/hailhq/voicebot/pipeline.py`) for an open-weight model servable via e.g. llama.cpp/vLLM/Ollama.
- [ ] Add local STT (e.g. whisper.cpp / faster-whisper) and local TTS (e.g. Piper / Coqui) options — today STT is Deepgram and TTS is Cartesia or ElevenLabs, all hosted-only and wired directly into `voicebot/hailhq/voicebot/pipeline.py` via LiveKit plugins (not as provider modules — `core/hailhq/core/providers/` only covers carrier-side voice (Twilio) and email (SES), no STT/TTS/LLM abstraction lives there).
- [ ] Benchmark the local stack: latency (time-to-first-audio, end-to-end turn latency), tokens/sec, WER for STT, and a comparison table against the current hosted config, run on stated consumer/prosumer hardware (the baseline r/LocalLLaMA expects).
- [ ] Once a local variant + benchmarks exist, revisit this draft and write the actual showcase post.
- [ ] Reddit account with enough karma/age to post in r/LocalLLaMA (check current subreddit posting requirements at submission time).
- [ ] Read r/LocalLLaMA's current rules/pinned rules thread before posting — self-promo and "look what I built" posts are only tolerated when the local/open-weight bar above is met.

## Steps to submit

This target cannot be submitted in Hail's current form. Do not post. If/when the prerequisites above are met:

1. Confirm a local/open-weight STT/TTS/LLM configuration is merged and documented (config flags, model names, quantization).
2. Run the benchmark suite from the TODO on real hardware and record numbers (latency, tokens/sec, WER, resource usage).
3. Re-draft the Content section below as a first-person "I built this, here's how it performs locally" post, with the benchmark table inline.
4. Go to https://www.reddit.com/r/LocalLLaMA/ and re-read the current rules (sidebar / pinned mega-thread) to confirm self-promotion format requirements haven't changed.
5. Log in to the Reddit account slated for this post; verify it meets the sub's minimum karma/account-age if such a rule exists.
6. Click "Create Post" → select the "Post" (text) type → paste the re-drafted title and body.
7. Attach the benchmark table as an image or a code-block/table in the post body if the sub prefers text over external links.
8. Preview the post, confirm links (GitHub, docs) resolve, then submit.
9. Monitor the thread for the first hour and respond to hardware/benchmark questions — this sub interrogates numbers closely.
10. Mark this row `status: confirmed-live` and add the permalink once it clears any automod/mod-queue review.

## Content

Not drafted. Writing a showcase post now would require claiming a local/open-weight stack Hail does not have, which violates both r/LocalLLaMA's rules (they will ask for exact model, quantization, and hardware, and will call out cloud-backed posts) and Hail's own claims policy (core-capability claims must be shipped and present-tense, never implied ahead of what's wired up). No copy is provided until the local variant and benchmarks exist.

When the blocker clears, the showcase post (not a listing/announcement) should follow this shape — a first-person "here's what I built" walkthrough of one concrete call handled fully on-device:

- **Title**: states the concrete local models used (STT/LLM/TTS + quantization), not "Hail" as the headline.
- **Body**: one paragraph on the use case demoed, the exact local model/quant/hardware for each of STT/LLM/TTS, the benchmark table (latency to first audio, end-to-end turn latency, tokens/sec, WER), a link to the config/docs so it's reproducible, and a link to the AGPLv3 repo — in that order of prominence, repo link last.
- **Required benchmark table columns**: stage, model+quant, hardware, metric, result — filled from a real run, never estimated.
- **Asset paths** (for a linked landing page only, never as the post's main image — this sub wants terminal/benchmark screenshots, not brand art): `/Users/r/playground/hail-website/public/assets/hail-monogram.svg` (dark bg), `hail-monogram-inverted.svg` (light bg), `og-card-1200x630.png` (share card if linking out).
- **Flair**: "Resources" or "Tutorial | Guide" (confirm exact current options on the sub before posting).

## Notes

- Current Hail voice pipeline (`voicebot/hailhq/voicebot/pipeline.py`) uses a hosted LLM fallback chain (OpenAI → Google → Anthropic) via LiveKit Agents, hosted STT (Deepgram) and hosted TTS (Cartesia or ElevenLabs, selected by which API key is configured) — all wired directly via LiveKit plugins in the voicebot pipeline itself. `core/hailhq/core/providers/` is a separate, narrower layer (carrier-side Twilio voice provisioning and SES email) and has no STT/TTS/LLM abstraction at all — its absence there doesn't mean Hail lacks STT, it means STT/TTS/LLM aren't structured as swappable "provider" modules today. None of this hosted stack is local or open-weight.
- r/LocalLLaMA is unusually strict about this: posts framed as product showcases but backed by hosted/closed APIs are typically downvoted or removed as thinly-veiled advertising.
- This is the one target in the list where the gating requirement is a genuine product gap, not a content/formatting gap — closing it means shipping a local inference option, not just writing better copy.
- Revisit once (a) a local/open-weight STT/TTS/LLM path is shipped and (b) benchmark numbers exist; until then leave `status: drafted` and treat the TODO's first line as the blocking condition.
