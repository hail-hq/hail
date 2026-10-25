# Voice LLM chain

The house chain (`voicebot/hailhq/voicebot/pipeline.py`, `_house_llms`) is
`ANTHROPIC_MODEL`, then `ANTHROPIC_BACKUP_MODEL`, then `OPENAI_MODEL`, then
`GOOGLE_MODEL` (Gemini) as the last backup. A model without its model id or
key is skipped (Anthropic needs `ANTHROPIC_BEDROCK_REGION` or
`ANTHROPIC_API_KEY`). Every Anthropic request runs with thinking off and
effort `low`, so the first word comes fast. Calls and texts use the same
chain.

Each model in the chain gets one try, with no retry (`max_retry_per_llm=0`
in `_fallback_chain`, used by `_house_llm` and by `build_llm` for an org LLM
with fallback). The
setting is one value for the whole LiveKit `FallbackAdapter`, not per model
(livekit-agents 1.8.8, `livekit/agents/llm/fallback_adapter.py`).

- `attempt_timeout=10.0` is not a total cap on a try. The adapter passes it
  as the request's `timeout`, and the Anthropic and OpenAI clients apply it
  to each httpx phase (connect, each read). A try fails after 10 s with no
  bytes; a reply that keeps streaming has no total limit.
- A failed model is marked unavailable and skipped on later turns. On a
  turn, the adapter walks the chain until a model answers (`_run`). Each
  model it passes without an answer gets one background request with that
  turn's chat (`_try_recovery`): an unavailable model it skips, and a model
  that just failed, right away. Models after the one that answers get none.
  A model never has two such requests at once. The model is back in the
  chain once such a request succeeds.

The classifier (`build_classifier` in `amd.py`) is `ANTHROPIC_BACKUP_MODEL`,
then `OPENAI_MODEL`, then `GOOGLE_AMD_MODEL`, with the same skip rule. Every
usable one is built: a fast failure (403, 429) leaves time for the next one.
`ANTHROPIC_MODEL` is the classifier only when none of these can be built. It
runs every yes/no check on a call: AMD, the pickup recheck, the unavailable
recheck and the phone menu check. It is built once per call and closes once,
at call end. It never uses the call's chain. With none of them, AMD is skipped
and the call talks. A per-call LLM, or an org LLM without fallback, is used
alone. An org LLM with fallback goes first, then every usable house
classifier: its fallback chain starts with Sonnet 5.5, which rejects a forced
tool.

`ANTHROPIC_MODEL` as the classifier works only when that model accepts a
forced tool (Haiku 5.5 does, Sonnet 5.5 does not, see Forced tools). With
Sonnet 5.5 there and no other classifier, every check fails with HTTP 400.

Each classifier gets one try of at most 3 s
(`CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS` in `amd.py`), then the next one runs.

- A check is capped at 5 s in total (`CLASSIFY_TIMEOUT_SECONDS` in
  `classify.py`). `ClassifierFailover` applies it to all its tries, so it
  also stops LiveKit's AMD call, which has no timeout of its own. A first
  try that times out leaves the next classifier 2 s. `classify_speech`
  applies the same cap to a customer's LLM used alone in the menu and
  unavailable checks. In AMD, that LLM has no timeout of its own: detection
  still ends 20 s after SIP answer (`POST_ANSWER_TIMEOUT_SECONDS`).
- The pickup recheck has one 5 s cap for transcription and classification
  together (`RECHECK_TIMEOUT_SECONDS` in `pickup.py`). A slow transcript can
  leave less than 3 s, so the backup classifier may not be reached.

```bash
# .env
OPENAI_MODEL=gpt-4.1
ANTHROPIC_MODEL=eu.anthropic.claude-sonnet-5-5
ANTHROPIC_BACKUP_MODEL=eu.anthropic.claude-haiku-5-5
ANTHROPIC_BEDROCK_REGION=eu-central-1
GOOGLE_MODEL=gemini-3.5-flash-lite
```

- `GOOGLE_MODEL` or `GOOGLE_API_KEY` empty: the chain skips Gemini.

- `ANTHROPIC_BEDROCK_REGION` set: the model runs on Amazon Bedrock in that
  region, signed with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. The `eu.`
  inference profile keeps requests in EU regions.
- Empty: Anthropic's API with `ANTHROPIC_API_KEY`, and `ANTHROPIC_MODEL` is a
  first-party id like `claude-haiku-5-5`.

## Bedrock access

1. The IAM policy in `infra/terraform/iam.tf` (`VoiceLLMBedrock`) allows any
   Bedrock model and any of the account's inference profiles. Apply it once:

   ```bash
   cd infra && terragrunt apply
   ```

2. A new Anthropic model on Bedrock needs a one-time Marketplace agreement in
   the account, or every request returns 403 `aws-marketplace:Subscribe`:

   ```bash
   aws bedrock list-foundation-model-agreement-offers \
     --region eu-central-1 --model-id anthropic.claude-haiku-5-5
   aws bedrock create-foundation-model-agreement \
     --region eu-central-1 --model-id anthropic.claude-haiku-5-5 \
     --offer-token <offerToken from the first command>
   ```

## Changing a model

Before you set a new id, list the provider's models and send one real request
with the deploy's own keys. A wrong id fails every call over to the next model.

- Thinking off: Sonnet 5.5 takes `between_tools`, Haiku 5.5 takes `disabled`
  (`_THINKING_OFF` in `pipeline.py`). Opus 5.5 cannot turn it off, so it runs
  with thinking on and answers slower.
- `GOOGLE_MODEL` must accept thinking level `minimal` (Gemini 3.7 and 3.8
  Flash return 400).
- A model missing from `_THINKING_OFF` runs with thinking `disabled` and logs
  one warning. Add the first-party id there; the region prefix, `anthropic.`
  and `-v1:0` are stripped before the lookup.
- Sonnet 5.5 can still think between tool calls. The LiveKit plugin drops
  those thinking blocks when it sends the history back. Anthropic accepts
  this: a tool result after an assistant turn without its thinking block
  returned 200 on Bedrock (2026-10-22).

## Forced tools

LiveKit's AMD classifier forces its tool (`tool_choice="required"` in
`livekit/agents/voice/amd/classifier.py`, livekit-agents 1.8.8), and so does
`classify_speech` in `classify.py`. Sonnet 5.5 on Bedrock rejects a forced
tool with HTTP 400 (`tool_choice: type "tool" and "any" are not supported for
this model.`), with or without thinking settings. So every check runs on the
classifier, which starts with `ANTHROPIC_BACKUP_MODEL` (Haiku 5.5), never on
Sonnet 5.5.

One real request per model and role, Bedrock `eu-central-1`, 2026-10-23:

| Role                                      | Model                            | Result                                                                        | Time         |
| ----------------------------------------- | -------------------------------- | ----------------------------------------------------------------------------- | ------------ |
| Chain                                     | `eu.anthropic.claude-sonnet-5-5` | 200                                                                           | 3.8 s        |
| Chain                                     | `eu.anthropic.claude-haiku-5-5`  | 200                                                                           | 1.5 s        |
| Chain                                     | `gpt-4.1`                        | 200                                                                           | 1.8 s        |
| AMD, forced tool                          | `eu.anthropic.claude-haiku-5-5`  | 200, `machine-vm`                                                             | 1.2 s        |
| AMD, forced tool                          | `gpt-4.1`                        | 200, `machine-vm`                                                             | 1.0 s        |
| AMD, forced tool                          | `gemini-3.1-flash-lite`          | 200 three times, one 504 (deadline expired)                                   | 0.6 to 6.4 s |
| AMD, forced tool                          | `eu.anthropic.claude-sonnet-5-5` | 400, forced tool not supported                                                | 0.5 s        |
| Menu check, forced tool, classifier chain | `eu.anthropic.claude-haiku-5-5`  | 6/7 200 on Haiku, labels right; 1 hit the 3 s try limit, gpt-4.1 answered 200 | 1.1 to 2.0 s |

`GOOGLE_MODEL` was empty, so Gemini was not tested in the chain.

## Prompt

The prompt has no rule for correcting a wrong word mid-sentence (LiveKit's
[self-corrections](https://docs.livekit.io/agents/start/prompting/#self-corrections-and-restarts)).
It was removed on purpose: test calls produced "Mike... I mean, <name>".

## Why this order

Six hard turns from real test calls (a misheard word that sounds like a name,
"send me this" with no channel, "email me", "I already asked", a lone
"thank you", "bye"), 4 runs each, graded automatically, 2026-10-20:

| Model                                 | Passed       | First token p50 / p90     |
| ------------------------------------- | ------------ | ------------------------- |
| Claude Sonnet 5.5 (Bedrock EU)        | 24/24, twice | 1.1 / 1.6 s               |
| Claude Haiku 5.5 (Bedrock EU)         | 20 to 22/24  | 1.0 to 1.2 / 1.4 to 2.0 s |
| GPT-4.1                               | 20/24        | 0.8 / 1.7 s               |
| GPT-5.5, GPT-5.6-sol (reasoning none) | 19/24        | 1.1 to 1.7 / 1.8 to 2.3 s |
| GPT-5.4-mini                          | 15/24        | 0.9 / 2.0 s               |
| Gemini 3.5 Flash Lite (minimal)       | 16/24        | 18 s (key over quota)     |

Prompt caching did not change the timings. The prompt is tuned on Claude,
which favours Claude here.
