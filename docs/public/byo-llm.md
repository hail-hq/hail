# Bring your own LLM

Point a Hail voice call at your own OpenAI-compatible endpoint. Your agent
becomes the brain of the call: Hail handles telephony, speech-to-text,
text-to-speech, turn detection, and tools, and asks your endpoint what to
say on every turn.

Two ways to do it. Per call, by passing an `llm` block to `POST /calls` —
different brains for different calls, or a different tenant's endpoint each
time. Or standing, by saving an endpoint once in the console — every call
your organization places uses it.

## Run one in five minutes

This is a complete endpoint. It has no dependency on Hail or on any model
SDK, and it runs as written.

```python
# byo_endpoint.py
import json, os, time, uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()


def generate_reply(messages: list[dict]) -> str:
    """Replace this with your agent. Return plain text.

    `messages` is the OpenAI-format history for the call so far: Hail's
    composed instructions as the leading system message, then alternating
    user and assistant turns. This stub echoes the last user turn, which is
    enough to prove the wiring without involving a real model.
    """
    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    return f"You said: {last_user}"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # Hail sends the api_key you configured as a bearer token. Check it —
    # your endpoint is reachable from the public internet.
    expected = os.environ.get("BYO_LLM_SECRET", "")
    if not expected or request.headers.get("authorization") != f"Bearer {expected}":
        return JSONResponse(status_code=401, content={"error": "invalid api key"})

    body = await request.json()
    model = body.get("model", "byo-example")
    reply = generate_reply(body.get("messages", []))

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    def sse(**fields) -> str:
        frame = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
        }
        frame.update(fields)
        return f"data: {json.dumps(frame)}\n\n"

    async def stream():
        yield sse(
            choices=[
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": reply},
                    "finish_reason": None,
                }
            ]
        )
        yield sse(choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
```

Start it:

```bash
pip install fastapi uvicorn
export BYO_LLM_SECRET=demo-secret
uvicorn byo_endpoint:app --port 8000
```

Confirm it answers before you involve Hail:

```bash
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer demo-secret" \
  -H "Content-Type: application/json" \
  -d '{"model":"demo","messages":[{"role":"user","content":"What is the weather today?"}]}'
```

```
data: {"id": "chatcmpl-f5cd09e477fc", "object": "chat.completion.chunk", "created": 1786038174, "model": "demo", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "You said: What is the weather today?"}, "finish_reason": null}]}

data: {"id": "chatcmpl-f5cd09e477fc", "object": "chat.completion.chunk", "created": 1786038174, "model": "demo", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}

data: [DONE]
```

Hail requires a public `https` URL, so expose the port through a tunnel —
`cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000` — and
use the `https` address it prints. Then place the call:

```bash
hail call +15551234567 \
  --llm-url https://your-tunnel.example.com/v1 \
  --llm-key demo-secret \
  --llm-model demo \
  --recipient-consent
```

Answer the phone, say something, and the agent repeats it back. Your
endpoint is now driving a live phone call.

Add `--prompt "…"` alongside the `--llm-*` flags to send your task prompt
too: Hail composes its voice preamble plus your prompt into the leading
`system` message your endpoint receives. Prompt and endpoint are
independent choices — at least one is required, both together is fine.

## The contract

Hail calls your endpoint once per voice turn. The request below is a real
capture, taken by pointing Hail's own client at a recording server.

```
POST /v1/chat/completions HTTP/1.1
authorization: Bearer <api_key>
content-type: application/json
accept: application/json
user-agent: LiveKit Agents/1.6.6 (python 3.13.12)
```

```json
{
  "messages": [
    {
      "role": "system",
      "content": "<Hail's voice preamble, then your system_prompt>"
    },
    {
      "role": "user",
      "content": "What's the weather in Paris, and can you end the call after?"
    }
  ],
  "model": "my-model",
  "stream": true,
  "stream_options": { "include_usage": true },
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
          "type": "object",
          "properties": {
            "city": { "type": "string", "description": "City name" }
          },
          "required": ["city"]
        }
      }
    }
  ]
}
```

Notes that follow from the capture, not from the spec:

- **The path is `{base_url}/chat/completions`.** A `base_url` of
  `https://you.example.com/v1` becomes
  `https://you.example.com/v1/chat/completions`. A trailing slash on
  `base_url` is safe — it does not produce a double slash.
- **`stream` is always `true`**, and `stream_options.include_usage` is
  always sent. Ignore the latter if you do not report usage.
- **`tools` carries Hail's agent tools** — `end_call`, `send_dtmf`,
  `send_sms`, and whatever else the call enabled — in OpenAI function
  format. Return a `tool_calls` delta to invoke one. Hail executes it and
  sends the result back on the very next request, as a `role: "tool"`
  message keyed by `tool_call_id`:

  ```json
  {
    "role": "tool",
    "tool_call_id": "call_1",
    "content": "The weather in Paris is sunny."
  }
  ```

  Restrict which tools a call may use with `--tools` / the `tools` field.

- **`tool_choice` is absent on the first turn**, and appears as
  `"auto"` on the request that follows a tool result. Treat it as optional
  and do not require it.
- Hail's client sends `x-stainless-*` headers. Do not reject unrecognized
  headers.

### What you must return

`text/event-stream`, one `data:` line per frame, each frame a JSON
`chat.completion.chunk`. The minimum that works is a single content chunk:

```
data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1730000000,"model":"m","choices":[{"index":0,"delta":{"role":"assistant","content":"Hello"},"finish_reason":null}]}
```

`data: [DONE]` and the `finish_reason: "stop"` frame are both **optional** —
closing the response body ends the turn cleanly. Send them anyway: they are
the OpenAI-compatible contract, and a future client may be stricter. Emit
many small content chunks rather than one large one; each chunk is spoken as
it arrives, so streaming token-by-token is what makes the agent sound
responsive instead of stalled.

## Rules and failure modes

**URLs must be `https` and publicly resolvable.** Hail checks the scheme
when you submit the call, then resolves the host and rejects private,
loopback, and link-local addresses — twice: once in the API, once again in
the voicebot at call time. A URL that resolves to a private address by the
time the call runs ends the call with `end_reason=provider_key_error`.

**There is no fallback.** You chose this brain deliberately, so Hail does
not silently substitute its own models when your endpoint fails. A standing
console endpoint can opt into fallback; a per-call endpoint cannot.

**Three consecutive failures end a per-call endpoint's call.** After three
non-recoverable errors in a row with no successful turn between them, Hail
speaks a short goodbye and hangs up with `end_reason=llm_endpoint_failed`,
rather than letting a dead endpoint burn the caller's minutes. A turn that
succeeds resets the count. An interruption is not a failure — barge-in
cancels the in-flight request and never counts against you. This give-up
is armed for per-call `llm` blocks only: a standing endpoint with fallback
off that keeps failing ends the call as `end_reason=agent_error` instead.

**A non-200 costs about four seconds.** Hail retries the turn three times
before giving up on it. The caller hears silence for that whole window, so
return a fast, valid reply on your own error paths rather than a 500.

**Your key is encrypted at rest.** The `api_key` is Fernet-encrypted before
it is written to call metadata, and decrypted only inside the voicebot.
Reads never return it.

## Per call

Every surface takes the same three fields.

```bash
curl -X POST https://api.hail.so/calls \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to": "+15551234567",
    "llm": {
      "base_url": "https://you.example.com/v1",
      "api_key": "demo-secret",
      "model": "demo"
    },
    "recipient_consent": true
  }'
```

```bash
hail call +15551234567 \
  --llm-url https://you.example.com/v1 \
  --llm-key demo-secret \
  --llm-model demo \
  --recipient-consent
```

```python
from hail import Client, LLMConfig

async with Client(api_key="sk-...") as client:
    call = await client.calls.create(
        to="+15551234567",
        llm=LLMConfig(
            base_url="https://you.example.com/v1",
            api_key="demo-secret",
            model="demo",
        ),
        recipient_consent=True,
    )
```

The MCP `place_call` tool takes the same `llm` object.

## Standing, for every call

Save an endpoint once in the console instead of sending it per call:
**Console → Calls → Providers → LLM → Configure**. Choose the
`OpenAI-compatible` provider, enter the base URL, model, and key, and use
**Test** to validate the key against the endpoint before you save it. Keys
are write-only — after saving, the console shows only the last four
characters and the date it was set.

The same page configures speech-to-text and text-to-speech. Transport stays
Hail's.

Standing config also offers **fallback**: when enabled, a failure of your
endpoint falls through to Hail's own models rather than ending the call.
Off by default, because silently billing Hail's models would defeat the
point of bringing your own.

### Configure it as code

The console is one client of `/providers`; the CLI and the SDK are the
others. The organization always comes from your API key — it is never a
path or body field, so a key can only reach its own config.

```bash
# Save the endpoint and activate it. '--key -' reads the key from stdin,
# so it never lands in your shell history.
printf '%s' "$MY_PROVIDER_KEY" | hail providers set llm \
  --provider openai-compatible \
  --base-url https://you.example.com/v1 \
  --model demo \
  --key -

hail providers list                     # all layers; keys show as …ABCD
hail providers test llm                 # probe the stored key, live
hail providers activate llm --provider anthropic
hail providers delete llm anthropic
```

```python
from hail import Client

async with Client(api_key="sk-...") as client:
    await client.providers.set(
        "llm",
        provider="openai-compatible",
        api_key="demo-secret",
        params={"base_url": "https://you.example.com/v1", "model": "demo"},
        fallback_enabled=False,
    )
    result = await client.providers.test("llm")
    print(result.status)  # "valid" | "invalid"
```

`tts` and `stt` take the same five verbs — `params` is what differs per
layer (`voice_id`/`model` for `tts`, `model` for `stt`). The canonical
per-layer schemas are `LLMParams` / `TTSParams` / `STTParams` in
[`core/hailhq/core/provider_config.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/provider_config.py);
the routes are `/providers` in
[`openapi/openapi.yaml`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml).

Keys are write-only on every one of these paths: `GET /providers` returns
`key_last4` and `key_set_at` and nothing else key-shaped. To rotate a key,
`set` the layer again with the new one; to edit the model or base URL
without resending the key, omit `--key` (SDK: omit `api_key`).

`set` is a partial write: it changes only the fields you send and leaves
every other saved field alone. So this swaps the model and nothing else —

```bash
hail providers set tts --provider cartesia --model sonic-3
```

— the row's `voice_id` and its fallback setting survive untouched. Send
`--voice-id` (SDK: `params={"voice_id": ...}`) when you do want to change
it, and `--fallback` / `--fallback=false` (SDK: `fallback_enabled=True` /
`False`) when you want to move the fallback flag; omit them to leave both
as they are. What the server validates is the merged result, so a partial
write can never leave an invalid config behind — it 422s instead.

Config is per provider: rows are keyed by `(organization, layer, provider)`,
so pointing a layer at a different `--provider` starts a fresh row rather
than inheriting the old provider's params. `fallback_enabled` is `false` on
a new row.

> The console writes the same rows through `/internal`, which keeps
> full-replace semantics — that is what lets a console user clear a field by
> emptying it. Merging is specific to the public `/providers` routes the CLI
> and SDK use.

## Which brain runs a call

| Mode                      | Source                           | Fallback                    |
| ------------------------- | -------------------------------- | --------------------------- |
| **B — per-call endpoint** | `llm` on `POST /calls`           | none                        |
| **C — standing endpoint** | `/providers` (console, CLI, SDK) | opt-in                      |
| **A — Hail's models**     | no `llm` block                   | OpenAI → Google → Anthropic |

Precedence is B, then C, then A. A per-call `llm` block overrides your
standing config for that one call.

The resolution logic is in
[`voicebot/hailhq/voicebot/pipeline.py`](https://github.com/hail-hq/hail/blob/main/voicebot/hailhq/voicebot/pipeline.py)
(`build_llm`), and the request schema is
[`LLMConfig`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/schemas.py).
