# Voice LLM chain

The house chain (`voicebot/hailhq/voicebot/pipeline.py`, `_house_llms`) is
`ANTHROPIC_MODEL`, then `ANTHROPIC_BACKUP_MODEL`, then `OPENAI_MODEL`, then
`GOOGLE_MODEL` (Gemini) as the last backup. A model without its model id or
key is skipped (Anthropic needs `ANTHROPIC_BEDROCK_REGION` or
`ANTHROPIC_API_KEY`). Every Anthropic request runs with thinking off and
effort `low`, so the first word comes fast. Calls and texts use the same
chain.

The classifier (`build_classifier` in `amd.py`) is `ANTHROPIC_BACKUP_MODEL`,
then `OPENAI_MODEL`, then `GOOGLE_AMD_MODEL`, with the same skip rule. It runs
every yes/no check on a call: AMD, the pickup recheck, the unavailable
recheck and the phone menu check. It is built once per call. It closes when
AMD ends, or at call end when a phone menu answered and the menu check still
uses it. It never uses the call's chain. With none of them, AMD is skipped
and the call talks. A per-call LLM, or an org LLM without fallback, is used
alone. An org LLM with fallback goes first, then the house classifiers: its
fallback chain starts with Sonnet 5.5, which rejects a forced tool.

Each check is capped at 5 s in total (`CLASSIFY_TIMEOUT_SECONDS` in
`classify.py`). Each classifier gets one 3 s try
(`CLASSIFIER_ATTEMPT_TIMEOUT_SECONDS`), so the next one gets the 2 s left.
The pickup recheck caps its transcription step separately, at 5 s.

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
