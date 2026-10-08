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

## Hand over to a person

Let the agent pass a live call to a contact you picked.

```bash
curl -X PATCH "$HAIL_API_URL/v1/agents/$AGENT_ID" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "handover_contacts": [
      {"contact_id": "'"$CONTACT_ID"'", "note": "billing questions or an angry caller"}
    ]
  }'
```

CLI: `hail agents update <id> --handover "<contact-id>=billing questions"`
(repeat the flag for more people; `--no-handover` clears the list).
MCP sets contacts at creation only: `create_agent(handover_contacts=[{"contact_id": "...", "note": "..."}])`.
There is no MCP update tool; change them with the CLI or the API.

- Up to 10 contacts. Each needs a phone number in a country Hail sells
  numbers in. It must pass the do-not-call and premium-rate checks, on save
  and again before dialing. `note` is 1-200 characters and tells the agent
  when to hand over.
- The agent gets the `transfer_call` tool. If `tools` is a list, saving
  contacts adds `transfer_call` to it and clearing them removes it; `null`
  (all tools) stays `null`. The agent never sees a phone number.
- The contact sees your Hail number as caller ID.
- The contact has 30 seconds to answer. If not, the agent comes back and
  offers to take a message.
- On answer, the agent says who is calling and why, then goes silent. If
  either side hangs up, the call ends.
- `max_duration_seconds` stops applying once the contact answers: the call
  runs until the caller or the contact hangs up.
- Billed as one call at the normal voice rate.
- Sends a [`call.transferred`](webhooks.md) webhook when the contact answers.
- `PATCH` with `[]` clears the list; omit the field (or send `null`) to leave it.

## What happens on an inbound call

1. The carrier sends the call to LiveKit. LiveKit creates a room and
   dispatches the voicebot ([setup](./self-host/livekit-cloud.md#4-inbound-calls)).
2. The voicebot reads the dialed number and the caller from the SIP
   participant and asks [`hailhq.core.inbound_calls`](../../core/hailhq/core/inbound_calls.py)
   who answers:
   - number not yours, a pool number, or a number on the wrong trunk: the call
     is dropped, no record;
   - no live voice agent (paused, or `voice_enabled: false`): the call is
     recorded as `failed`, `end_reason` `no_agent`, and `call.failed` is sent;
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
   `system_prompt` and the last 20 texts of the last 24 hours with the caller
   (plus texts between that number and the caller that have no agent: sent
   through `POST /sms`, or received before the number had a text agent). Older
   texts and calls it reads with the `thread_history` tool (see [Threads](#threads)).
   The reply is sent from the same number through its carrier. The agent sends at most 20
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

## Threads

A caller texts an order number to the agent's SMS number, then calls the agent.
When the caller asks about it, the agent looks it up with the `thread_history`
tool. A text sent during a call goes to the voice agent and is then marked
`done`. A text sent from the moment the call row is created (ringing included)
counts. If the voice agent never got it, the text agent answers it when the
call ends, or when the stale-call sweep closes the call. The reply age limit
(`HAIL_TEXT_REPLY_MAX_AGE_SECONDS`) restarts then.

- Scope: agent + caller number. Texts and call turns both count.
- Prompts carry no history. Both agents read it with the `thread_history` tool:
  `source` is `sms` (texts), `voice` (call turns) or `all`; `before` pages back
  over the last 7 days; `item_id` returns one full message. The voice prompt
  gets one line telling the agent to use the tool when the call has it; the
  text prompt always has it. Why: history in the prompt also held the agent's
  own past "I don't have that" lines, and the model repeated them.
- The text agent's chat holds the last 20 texts of the last 24 hours, no call
  turns. `thread_history` is its only tool.
- `thread_history` also returns texts between the number and the caller that
  have no agent.
- Hidden or invalid caller numbers get no history.
- Caller ID on a phone call can be faked. Do not put secrets in an agent's
  instructions or in texts it sends.
- History the tool returns is sent to the agent's LLM, including a bring-your-own endpoint.
- `send_sms` picks the number in this order:
  1. the dialed number, if it can text and is not bound to another agent.
     Bound to this agent: used. Free: it is bound to this agent when the agent
     has `sms_enabled`, else used unbound;
  2. the oldest org SMS number bound to this agent;
  3. the oldest free org SMS number: bound to this agent when it has
     `sms_enabled`, else used unbound;
  4. none: the agent says it cannot text.

  It never uses a number bound to another agent. A number bound this way
  makes the agent answer every text sent to it (audit entry `number.route`).
  A number used unbound stays unrouted.

Code: [`threads.py`](../../core/hailhq/core/threads.py),
[`text_watch.py`](../../voicebot/hailhq/voicebot/text_watch.py),
[`thread_history.py`](../../core/hailhq/core/agent_tools/thread_history.py).

## Routing rules

- `voice_agent_id` needs the `voice` capability and an agent with
  `voice_enabled: true`; otherwise the request returns 422. Setting it attaches the
  number for inbound at the carrier and lists it on Hail's LiveKit inbound
  trunk for that carrier ([`hailhq.core.inbound_routing`](../../core/hailhq/core/inbound_routing.py));
  `null` undoes both. `PhoneNumberResponse.inbound_registered` says where it stands.
- `sms_agent_id` needs the `sms` capability and an agent with
  `sms_enabled: true`; otherwise the request returns 422. No carrier work: inbound texts
  already reach Hail.
- Releasing a number, or deleting its agent, unregisters it first.
- A paused agent (`status: paused`) answers nothing; calls to its numbers
  fail with `no_agent`, and `POST /calls` with its `agent_id` returns 409. `POST /calls` with an agent
  that has `voice_enabled: false` also returns 409.
