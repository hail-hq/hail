# Call controls

What you can set on a voice call, who runs the conversation, how the call
ends, and what your app hears while it runs.

```bash
curl -X POST "$HAIL_API_URL/v1/calls" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "to": "+14155550100",
    "agent_id": "a1b2...",
    "system_prompt": "Confirm order A1001 ships Friday. Do not offer refunds.",
    "first_message": "Hi, I am calling about your order.",
    "tools": ["end_call", "send_sms"],
    "voice_config": {"language": "es"},
    "recording_enabled": false,
    "metadata": {"ticket": "T-1001"},
    "recipient_consent": true
  }'
# → {"id": "0d9c...", "status": "queued", ...}
```

Schema: `CallCreate` in [`openapi/openapi.yaml`](../../openapi/openapi.yaml).

## What you set on each call

Every field on `POST /v1/calls` applies to that call only.

- `system_prompt`: the task and the rules for this call.
- `first_message`: the opening line. Omit it and the agent waits for the
  other person to speak.
- `llm`: your own model for this call. See [below](#who-runs-the-conversation).
- `tools`: the built-in tools the agent may use on this call, such as
  `end_call`, `send_sms` or `send_dtmf`. `[]` allows none. Omitted, the
  agent's list applies, or every tool your workspace supports. The agent's
  [HTTP tools](agents.md#call-your-api-http-tools) stay on either way.
- `voice_config`: `voice_id` and `language` (one of the
  [39 languages](languages.md)).
- `recording_enabled`: `true` or `false` for this call. Omitted, the agent
  and then the workspace setting decide.
- `ai_disclosure`: speak the AI line first (default `true`).
- `metadata`: any JSON object. Hail stores it and returns it on reads. The
  model does not see it. Put what the agent must know in `system_prompt`.
- `agent_id`: start from a saved [agent](agents.md). Any field you send on
  the call wins over the agent's value.

These are set on the [agent](agents.md) or in the workspace call settings,
not per call:

- `max_duration_seconds` (60 to 3600): the call length limit. Hail can also
  shorten it to what the workspace balance covers.
- `handover_max_duration_seconds` (60 to 3600, default 1800): the limit once
  a person takes the call.
- `handover_contacts`: the people the agent can pass the call to.

A live call keeps the settings it started with. Changing the agent changes
the next call, not the one in progress.

## Who runs the conversation

Pick one of three setups. Hail runs the phone line, speech to text, text to
speech, and turn taking in all three.

1. **Hail's model with your instructions.** Send `system_prompt`, or an
   `agent_id`. Hail's model decides what to say.
2. **Hail's model plus your API.** Add [HTTP tools](agents.md#call-your-api-http-tools)
   to the agent. During the call, Hail sends a signed POST to your URL with
   the arguments the model chose and the `call_id`. Your answer goes back to
   the model. `timeout_seconds` is 1 to 10 (default 5). `wait_message` is
   said while Hail waits. A timeout, a non-2xx status, or an unreachable
   host gives the model a short error, and the call goes on. Your API keeps
   its own permissions, so it decides what an action may do.
3. **Your own model.** Send an `llm` block, or save an endpoint for the
   workspace. Hail calls your OpenAI-compatible endpoint on every turn with
   the conversation as text. Your model returns text and Hail speaks it.
   See [Bring your own LLM](byo-llm.md).

Hail exchanges text with your model. It has no endpoint that streams raw
call audio to or from your app.

## How a call ends

- **The agent hangs up.** The `end_call` tool ends the call after the agent
  says goodbye. The agent uses it when the other person is done or asks to
  stop. On outbound task calls it can also end once the task is done.
  `end_reason` is `normal_hangup`.
- **The other person hangs up.** `end_reason` is `normal_hangup`.
- **The time limit is reached.** At `max_duration_seconds` the agent says a
  short closing line and stops. `end_reason` is `soft_cap_reached`.
- **Voicemail answers an outbound call.** Hail hangs up without leaving a
  message. `status` is `no_answer` and `end_reason` is `voicemail_reached`.
  See [answering machine detection](architecture.md#answering-machine-detection-and-dtmf).
- **Your own model keeps failing.** After three failed turns in a row on a
  per-call `llm`, Hail says goodbye and ends the call with
  `end_reason=llm_endpoint_failed`. See [failure modes](byo-llm.md#rules-and-failure-modes).

The public API has no endpoint to hang up a call that is in progress.
Remove `end_call` from `tools` if the agent must never hang up by itself.

## What your app hears

While the call runs:

- Webhooks: `call.received` (inbound), `call.answered`, `call.transferred`.
- `GET /v1/calls/{call_id}` returns `status` and the transcript turns so far.
  The transcript grows while the call runs.
- `GET /v1/events?id=call:<call_id>` walks the call's events in order. Pass
  `next_cursor` to keep reading.

After the call:

- Webhooks: `call.completed`, `call.failed`, `call.busy`, `call.no_answer`.
- [`call.summarized`](webhooks.md#call-summarized): the full transcript and
  a short summary.
- `GET /v1/calls/{call_id}`: the final `status`, `end_reason`, and transcript.

All event names and payloads: [Webhooks](webhooks.md#event-types).

## Phone menus, handover and voicemail

- **Phone menus.** The agent presses keypad keys with `send_dtmf`, at any
  point in the call. See [answering machine detection and DTMF](architecture.md#answering-machine-detection-and-dtmf).
- **Handover.** With `handover_contacts` set, the agent passes a live call to
  a person with `transfer_call`. See [Hand over to a person](agents.md#hand-over-to-a-person).
- **Voicemail.** Outbound calls detect voicemail and hang up without a message.
