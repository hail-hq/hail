# Agents

An agent is a saved brain: instructions, greeting, AI line, voice, tools and
limits. A number routes its incoming calls to one agent and its incoming texts
to one agent. `POST /calls` can place outbound calls with the same agent.

```bash
# 1. Save an agent.
curl -X POST "$HAIL_API_URL/v1/agents" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "name": "Front desk",
    "system_prompt": "You are the front desk of Acme Dental. Book, change or cancel appointments.",
    "first_message": "Thanks for calling. Are you booking, changing, or cancelling an appointment?",
    "voice_config": {"language": "en"}
  }'
# → {"id": "a1b2...", ...}

# 2. Point a number at it. Calls and texts to the number are now answered.
curl -X PATCH "$HAIL_API_URL/v1/numbers/$NUMBER_ID" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"voice_agent_id": "a1b2...", "sms_agent_id": "a1b2..."}'

# 3. Or place an outbound call with it.
curl -X POST "$HAIL_API_URL/v1/calls" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"to": "+14155550100", "agent_id": "a1b2...", "recipient_consent": true}'
```

CLI: `hail agents create "Front desk" --prompt-file ./front-desk.md`, then
`hail numbers route <number-id> --calls <agent-id> --texts <agent-id>`.
Restrict tools with `hail agents update <id> --tools end_call,send_sms`; allow
all again with `hail agents update <id> --all-tools`.
MCP: `list_agents`, `create_agent`, `route_number`, and `place_call(agent_id=...)`.
SDK: `client.agents.create(...)`, `client.numbers.route(...)`.

Schemas: `AgentCreate`, `AgentUpdate`, `AgentResponse`, `PhoneNumberRoutingUpdate`
in [`openapi/openapi.yaml`](../../openapi/openapi.yaml).

## What happens on an inbound call

1. The carrier sends the call to LiveKit. LiveKit creates a room and
   dispatches the voicebot ([setup](./self-host/livekit-cloud.md#4-inbound-calls)).
2. The voicebot reads the dialed number and the caller from the SIP
   participant and asks [`hailhq.core.inbound_calls`](../../core/hailhq/core/inbound_calls.py)
   who answers:
   - number not yours, a pool number, or a number on the wrong trunk: the call
     is dropped, no record;
   - no live voice agent: the call is recorded as `failed`, `end_reason`
     `no_agent`, and `call.failed` is sent;
   - no credits, or voice suspended: `failed` with `insufficient_funds`
     (or `user_rejected`), and `call.failed`;
   - otherwise a `ringing` call, `call.received`, then the agent speaks.
3. The agent speaks the AI line, then `first_message` (or waits), then
   follows `system_prompt` with the agent's tools. `call.answered` and
   `call.completed` follow as on outbound calls. Each answered minute costs
   the same as an outbound minute.

The AI line on inbound calls defaults to
`Hi, this is an AI assistant answering on behalf of {org}.`; the workspace
default lives in Call settings (`organization_call_settings.ai_disclosure_line`)
and an agent can set its own `ai_disclosure_line`. `ai_disclosure: false` on the
agent skips it; the responsibility for that is yours.

## What happens on an inbound text

1. Hail stores the text and sends `sms.received` as before.
2. `STOP`, `CANCEL`, `END`, `QUIT`, `UNSUBSCRIBE` and `HELP`/`INFO` are answered by Hail and never reach
   the agent. `YES`, `START` and `UNSTOP` are an opt-in only from a person who opted out; Hail
   turns texts back on and replies. From anyone else they are an answer, and the agent gets them.
3. Any other text to a number with `sms_agent_id` set (agent `live`,
   `sms_enabled: true`) is answered by the agent: one reply, written from
   `system_prompt` and the last 20 messages of the thread (24 hours), sent
   from the same number through its carrier. The agent sends at most 20
   replies per thread in any 24 hours; past that it stays quiet until older
   replies leave the window. Replies bill as outbound SMS. Inbound rows carry `agent_reply_state`
   (`pending`, `processing`, `done`, `skipped`, `failed`) in the database;
   replies carry `agent_id`.

The text worker runs inside the voicebot service
([`hailhq.voicebot.textbot`](../../voicebot/hailhq/voicebot/textbot.py)) and
needs `HAIL_INTERNAL_SECRET` and `HAIL_API_URL`. It answers
`HAIL_TEXT_REPLY_CONCURRENCY` texts at once. A failed model or API call is
retried up to 3 times with backoff; a text still unanswered after
`HAIL_TEXT_REPLY_MAX_AGE_SECONDS` is skipped.

## Routing rules

- `voice_agent_id` needs the `voice` capability. Setting it attaches the
  number for inbound at the carrier and lists it on Hail's LiveKit inbound
  trunk for that carrier ([`hailhq.core.inbound_routing`](../../core/hailhq/core/inbound_routing.py));
  `null` undoes both. `PhoneNumberResponse.inbound_registered` says where it stands.
- `sms_agent_id` needs the `sms` capability. No carrier work: inbound texts
  already reach Hail.
- Releasing a number, or deleting its agent, unregisters it first.
- A paused agent (`status: paused`) answers nothing; calls to its numbers
  fail with `no_agent`, and `POST /calls` with its `agent_id` returns 409.
