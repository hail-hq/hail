# Voice LLM chain

The house chain (`voicebot/hailhq/voicebot/pipeline.py`, `_house_llms`) is
Anthropic, then OpenAI, then Google. Every Anthropic request runs with thinking
off and effort `low`, so the first word comes fast.

```bash
# .env
ANTHROPIC_MODEL=eu.anthropic.claude-haiku-5-5
ANTHROPIC_BEDROCK_REGION=eu-central-1
GOOGLE_MODEL=gemini-3.5-flash-lite
```

- `ANTHROPIC_BEDROCK_REGION` set: the model runs on Amazon Bedrock in that
  region, signed with `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. The `eu.`
  inference profile keeps requests in EU regions.
- Empty: Anthropic's API with `ANTHROPIC_API_KEY`, and `ANTHROPIC_MODEL` is a
  first-party id like `claude-haiku-5-5`.

## Bedrock access

1. The IAM policy in `infra/terraform/iam.tf` (`VoiceLLMBedrock`) allows
   `ANTHROPIC_MODEL` and its inference profile, read from `.env` when
   `ANTHROPIC_BEDROCK_REGION` is set. Apply it after merge:

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

- `ANTHROPIC_MODEL` must accept `thinking: disabled` at effort `low` (Haiku
  5.5 does; Sonnet 5.5 and Opus 5.5 return 400).
- On Bedrock, the IAM policy allows only the `ANTHROPIC_MODEL` in `.env`.
  After you change it, run `cd infra && terragrunt apply`, or every request
  returns AccessDenied.
- `GOOGLE_MODEL` must accept thinking level `minimal` (Gemini 3.7 and 3.8
  Flash return 400).
