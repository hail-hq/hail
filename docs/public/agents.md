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
MCP: `list_agents`, `get_agent`, `create_agent`, `update_agent`, `delete_agent`, `route_number`, and `place_call(agent_id=...)`.
SDK: `client.agents.create(...)`, `client.numbers.route(...)`.

Schemas: `AgentCreate`, `AgentUpdate`, `AgentResponse`, `PhoneNumberRoutingUpdate`
in [`openapi/openapi.yaml`](../../openapi/openapi.yaml).

## Hand over to a person

Let the agent pass a live call to a person you picked: a team member with a
phone number, or a contact. Use the `id` that `GET /contacts` (or
`hail contacts list`) shows: a contact's uuid, or `member:<user id>` for a
team member.

```bash
curl -X PATCH "$HAIL_API_URL/v1/agents/$AGENT_ID" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "handover_contacts": [
      {"contact_id": "member:6f1c2a9e-3b7d-4c51-9a8e-2d4f6b1e0c37", "note": "sales questions"},
      {"contact_id": "'"$CONTACT_ID"'", "note": "billing questions or an angry caller"}
    ]
  }'
```

CLI: `hail agents update <id> --handover "member:<user-id>=sales questions"`
(repeat the flag for more people; `--no-handover` clears the list).
MCP: `create_agent(handover_contacts=[{"contact_id": "...", "note": "..."}])` sets the list;
`update_agent(agent_id, handover_contacts=[...])` replaces it, and `[]` removes every contact.

- Up to 10 people. Each needs a phone number in a country Hail sells
  numbers in. A team member must belong to the organization; one who leaves
  it is skipped and cannot be dialed. It must pass the do-not-call and premium-rate checks, on save
  and again before dialing. `note` is 1-200 characters and tells the agent
  when to hand over.
- The agent gets the `transfer_call` tool. If `tools` is a list, saving
  contacts adds `transfer_call` to it and clearing them removes it; `null`
  (all tools) stays `null`. The agent never sees a phone number.
- The contact sees your Hail number as caller ID.
- The contact has 30 seconds to answer. If not, the agent comes back and
  offers to take a message. While it rings the agent keeps listening: the
  caller can say stop, or ask for someone else, and the agent hangs up the
  ringing phone (`cancel_transfer`, which comes with `transfer_call`).
- While the phone rings, the contact gets a text from your Hail number and
  an email with the caller's number and the reason (links, emails and phone
  numbers removed), once per call. A text from a US number to a US number
  needs an approved US SMS registration; without one the call still rings,
  with no text. The email needs a verified email domain and an email on the
  contact (a team member's login email); without one, no email.
- A plain forward: when the phone picks up, the two people are connected
  and the agent stops listening and speaking. It says nothing to the
  contact. If voicemail answers, the caller hears it and can leave a
  message. If nobody on the contact's side makes a sound for 15 seconds
  (some mailboxes answer in silence), or the line hangs up within 10
  seconds of its first sound (a carrier message), the agent hangs up that
  line and comes back to the caller. Code: [`handover_watch.py`](../../voicebot/hailhq/voicebot/handover_watch.py).
  Once connected, if either side hangs up, the call ends.
- `max_duration_seconds` stops applying once the contact answers.
  `handover_max_duration_seconds` (60..3600, default 1800 = 30 minutes;
  CLI `--handover-max-minutes`) counts from the answer. When it is reached
  the agent says goodbye and the call ends for both people. Either person
  can hang up sooner. On Hail's cloud the limit is also cut to the minutes
  your credits pay for. A handover is refused when your credits leave less
  than one minute of talk, after about 30 seconds for the pickup check and
  10 seconds for the goodbye.
- Billed as one call at the normal voice rate. The text and the email to the
  contact are billed as a normal SMS and email.
- Sends a [`call.transferred`](webhooks.md) webhook once someone on the
  contact's side has spoken and stayed on the line for 10 seconds.
- `PATCH` with `[]` clears the list; omit the field (or send `null`) to leave it.

## Email the caller

Let the agent email the person on a call something they asked for, such as a
datasheet link or a short follow-up, at an address they say on the call.
Off by default.

```bash
curl -X PATCH "$HAIL_API_URL/v1/agents/$AGENT_ID" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{"email_caller_enabled": true}'
```

CLI: `hail agents update <id> --email-caller` (`--email-caller=false` turns it off).
MCP: `create_agent(email_caller_enabled=true)` or `update_agent(agent_id, email_caller_enabled=true)`.
SDK: `client.agents.update(agent_id, email_caller_enabled=True)`.

- The agent gets the `email_caller` tool on phone calls with a known caller
  number. It reads the address back and sends only after the person says yes.
  The switch alone decides: the `tools` list does not turn it on or off, and
  `email_caller` is not a `tools` name (naming it returns 422).
- One recipient, no CC or BCC. Plain text and links, up to 2,000 characters;
  no attachments.
- Hail adds fixed words the agent cannot change: the subject starts with
  `From a phone call with <your organization>:`, the body starts with
  `You asked for this during a phone call with <your organization>.`, and a
  footer says the address was given during that call.
- Relay risk: anyone who can call the number can ask for an email to any
  address, with text they dictate, from your domain. Hail caps it:
  - 2 emails per call, both to one address. Fix a misheard address before
    the send: the agent reads it back first;
  - 3 emails and 2 addresses per phone number of the other person, in any
    24 hours, across calls;
  - 3 emails to one address in any 24 hours, from any caller;
  - 100 emails per organization in any 24 hours.

  Each cap counts only your organization's emails.

  Each check takes the stricter reading. The per-address cap counts email
  inboxes:
  `+tags` are ignored, and Gmail dots too (`n.ame+1@gmail.com` is
  `name@gmail.com`). The per-call and per-number address rules count
  spellings: `a+x@example.com` and `a@example.com` are two addresses. An
  address that is only a `+tag` is refused. The email goes to the address
  as given. A bounced email still counts, but its address
  can be replaced. A send
  that may have reached the provider counts even if it failed. A caller
  with a hidden number gets no email. Caller ID can be faked, so the
  per-number caps slow one person down but do not stop a determined one; the
  per-address and organization caps still hold.

- The agent's later threads leave these emails out: their text came from the
  caller, so they are never quoted back as your team's mail.

- Sent from the same address as the agent's other email: your verified custom
  domain (the oldest, if you have several), else your hail-mail address.
- The same checks as any email: suppression list, bounce and complaint rules,
  sending limits. Billed as a normal outbound email; with no credit it is not sent.
- Each send adds an `email_caller` event to the call (`GET /v1/events?id=call:<call id>`,
  or `hail tail <call id>`) with `email_id`, `subject`, and `status` right
  after the send. Later changes (bounce, complaint) are on the email's own
  events: `GET /v1/events?id=email:<email_id>`.
- The address is whatever the person says. Hail cannot prove it is theirs.
  It must be one plain ASCII mailbox (no quotes, brackets or spaces).
- The call event and the tool record do not copy the address or the body.
  They keep the subject, which the caller may have dictated. The call's
  transcript still holds what was said aloud.
- Turning it on applies from the next call. Turning it off, or pausing the
  agent, stops emails at once, also on a call in progress.

Code: [`email_caller.py`](../../core/hailhq/core/agent_tools/email_caller.py).

## Call your API (HTTP tools)

Let the agent call your own API in the middle of a call, text or email, for
example to look up an order. Hail POSTs the arguments the model chose, signed,
and gives your answer back to the model.

```bash
curl -X PATCH "$HAIL_API_URL/v1/agents/$AGENT_ID" -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "http_tools": [{
      "name": "lookup_order",
      "description": "Look up an order by its number. Use it when the caller asks where an order is.",
      "url": "https://api.example.com/hail/orders",
      "parameters": {
        "type": "object",
        "properties": {"order_number": {"type": "string", "description": "The order number."}},
        "required": ["order_number"]
      },
      "headers": {"Authorization": "Bearer sk_live_example"},
      "timeout_seconds": 5,
      "wait_message": "One moment, let me check."
    }]
  }'
# → {..., "http_tools": [...], "http_tool_secret": "hts_..."}  (store the secret now)
```

CLI: `hail agents update <id> --http-tools-file ./tools.json` (a JSON array;
`--no-http-tools` clears it), `hail agents rotate-tool-secret <id>`.
MCP: `create_agent(http_tools=[...])`, `update_agent(agent_id, http_tools=[...])`.
SDK: `client.agents.create(..., http_tools=[...])`, `client.agents.rotate_tool_secret(id)`.
Schema: `HttpToolIn` in [`openapi/openapi.yaml`](../../openapi/openapi.yaml).

Your endpoint receives:

```json
{
  "id": "6b0e…",
  "type": "tool.call",
  "created_at": "2026-11-18T10:00:00+00:00",
  "tool": "lookup_order",
  "arguments": { "order_number": "A1001" },
  "agent_id": "a1b2…",
  "organization_id": "…",
  "channel": "call",
  "call_id": "0d9c…",
  "sms_id": null,
  "email_id": null
}
```

`channel` is `call`, `sms` or `email`; the matching id is the call, or the
inbound text or email the agent is answering. Headers: `X-Hail-Signature`,
`X-Hail-Tool` (the tool name), `X-Hail-Request` (the `id`), and your own
static headers. Verify the signature exactly like a
[webhook](webhooks.md#verify-the-signature-python), with the agent's
`http_tool_secret`:

```python
import hashlib, hmac, time

def verify(secret: str, signature_header: str, raw_body: bytes) -> bool:
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    if abs(time.time() - int(parts["t"])) > 300:  # refuse old requests
        return False
    mac = hmac.new(secret.encode(), f"{parts['t']}.".encode() + raw_body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), parts["v1"])
```

- Up to 10 tools per agent. `name`: a letter, then letters, digits or `_`;
  built-in tool names are refused. `parameters`: a JSON Schema of type
  `object` (the supported keywords are listed on `HttpToolIn`).
- `url` must be `https` and resolve to public addresses. Private, loopback,
  link-local and carrier-NAT addresses are refused on save and checked again
  on every request, at connect time. Redirects are not followed.
  A `url` whose host is private or does not resolve gets a 422 on save with
  the same text either way: `URL host is private or could not be resolved`.
- `headers` are stored encrypted; responses show only `header_names`. A
  `PATCH` replaces the whole list; a tool sent without `headers` keeps the
  saved headers of the tool with the same name, and `{}` removes them.
  Saved headers are kept only while the `url` keeps its scheme, host and
  port; a `url` on a new host needs `headers` again (422 otherwise).
  A renamed tool is a new tool: send its `headers` again.
- Answer with JSON or text. The model gets up to 4,000 characters. A non-2xx
  status, a timeout (`timeout_seconds`, 1..10, default 5) or an unreachable
  host gives the model a short error; the call goes on.
- `wait_message` is said on calls while Hail waits, not in texts or email.
- HTTP tools are on whatever `tools` says; `tools` lists built-in tools only.
- A request can repeat when a text or email reply is retried, with a new
  `id`, and the arguments can differ the second time. Make lookups safe to
  run twice. For a tool that changes something, check on your side whether
  that change (for example, the same order number) was already made for that
  `sms_id` or `email_id`.
- `http_tool_secret` is returned once: on the first save with HTTP tools, and
  by `POST /v1/agents/{id}/rotate-tool-secret`. Live calls keep the secret
  they started with.
- The server needs `HAIL_PROVIDER_SECRET_KEY` (self-host); without it, saving
  HTTP tools returns 503.
- Code: [`http_tool.py`](../../core/hailhq/core/agent_tools/http_tool.py).

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
   follows `system_prompt` with the agent's tools. With a `first_message`
   and no custom AI line, the agent speaks one greeting instead: the first
   sentence of `first_message`, then `I'm an AI assistant for {org}.`, then
   the rest. `call.answered` and
   `call.completed` follow as on outbound calls. Each answered minute costs
   the same as an outbound minute. After the call,
   [`call.summarized`](webhooks.md#call-summarized) sends the transcript and a
   short summary, and `GET /v1/calls/{id}` returns the transcript.

The AI line on inbound calls defaults to
`Hi, this is an AI assistant answering on behalf of {org}.` (or the short
line inside `first_message`, above); the workspace
default lives in Call settings (`organization_call_settings.ai_disclosure_line`)
and an agent can set its own `ai_disclosure_line`. `ai_disclosure: false` on the
agent skips it; the responsibility for that is yours.

The built-in lines are spoken in the call's language (`voice_config.language`,
else the workspace voice's language). A custom `ai_disclosure_line` is spoken
as written.

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
`HAIL_TEXT_REPLY_MAX_AGE_SECONDS` is skipped. A text to a workspace with no
credits gets no reply. If the balance is under 1 cent when the worker picks
the text up, it is skipped before the model runs, with
`metadata.skipped_reason: "insufficient_funds"`. If credits run out while the
model writes, the text is skipped with the same reason.

A text that gets no reply names why in `metadata.skipped_reason`:

| Reason               | Meaning                                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------------------- |
| `insufficient_funds` | The workspace has no credits.                                                                                 |
| `no_agent`           | The number has no live text agent any more.                                                                   |
| `expired`            | The text waited longer than `HAIL_TEXT_REPLY_MAX_AGE_SECONDS`.                                                |
| `empty_reply`        | The model wrote no reply.                                                                                     |
| `thread_cap`         | The thread reached its reply limit.                                                                           |
| `send_blocked`       | A send check refused the reply (opt-out, blocked number, rate limit, or texting suspended for the workspace). |
| `sms_registration`   | The number needs registration before it can text this person. `metadata.skipped_detail` says how.             |
| `agent_caps`         | The agent reached its sending limit.                                                                          |
| `active_call`        | A call with this person was in progress. The text is answered after the call.                                 |

A failed reply can also carry a `skipped_reason` from the send step:

| Reason             | Meaning                                                                                   |
| ------------------ | ----------------------------------------------------------------------------------------- |
| `provider_error`   | The carrier could not be reached to send the reply.                                       |
| `carrier_rejected` | The carrier refused the reply. The reply's `error_code` holds the carrier's code.         |
| `reply_failed`     | The reply failed for another reason: model or send errors after 3 tries, or no model key. |

## Answer email with an agent

```bash
hail email domain route <domain-id> --agent <agent-id>
```

The same call over the API is `PATCH /v1/email-domains/{id}` with `email_agent_id`.
Send `null` to stop replies. The agent must be `live` with `email_enabled: true`.

`email_agent_scope` says who the agent answers:

- `members` (default): verified members of the organization.
- `anyone`: every sender. A stranger can then make the agent use its tools, so choose this
  only for an agent that is safe to reach from the internet.

A new agent on an address starts at `members` unless the call names a scope.

Under both, the mail must pass DMARC (`dmarc_verdict: PASS`) with one well-formed `From`
address, because anyone can write a `From` header. Other mail gets no reply, so a sender on a
domain with no DMARC policy gets none.

A hail-mail address always receives mail, so the route is its only switch. A custom domain must
be verified with inbound and sending on.

What happens on inbound mail:

1. Hail stores the mail and sends `email.received` as before.
2. The agent answers once, to the `From` address, in the same thread. A hail-mail reply comes
   from the address itself, and a custom-domain reply from `noreply@<domain>`. The agent reads
   its instructions and the last 20 proven messages of the thread.
3. Hail never answers spam, viruses, auto-replies, list mail, `noreply` senders, or other Hail
   addresses. Marketing mail sent through Hail to one person carries `List-Unsubscribe`, so it counts as
   list mail. Informational mail does not. For one sender in any 24 hours, the agent sends at most 20 messages, counting
   replies and tool sends.
4. With no credit, the mail is stored and the agent stays quiet. The row carries
   `suppressed_reasons: ["insufficient_funds"]`.

Inbound rows carry `agent_reply_state` (`pending`, `processing`, `done`, `skipped`, `failed`).
A skipped row names why in `metadata.agent_skip_reason`. Replies bill as outbound email and
carry `agent_id`.

An agent answering email can use `list_contacts`, `send_email`, and `send_sms`, unless its
`tools` list leaves them out. Each send names a person in the directory, and each email allows
at most 3 sends.

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
  `source` is `sms` (texts), `voice` (call turns) or `all`.
  `query` searches all stored history for this agent and caller, including older conversations.
  `before` pages through older results; `item_id` returns one full message.
  Without a query or cursor, the tool starts with recent messages. This is not a retention limit.
  The voice prompt
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
