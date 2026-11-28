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
Restrict tools with `hail agents update <id> --tools send_sms`; allow
all again with `hail agents update <id> --all-tools`. Some tools stay on
every call, whatever the list says: `end_call` (hang up), `send_dtmf`
(keypad presses) and Hail's settings guide.
MCP: `list_agents`, `get_agent`, `create_agent`, `update_agent`, `delete_agent`, `route_number`, and `place_call(agent_id=...)`.
SDK: `client.agents.create(...)`, `client.numbers.route(...)`.

Schemas: `AgentCreate`, `AgentUpdate`, `AgentResponse`, `PhoneNumberRoutingUpdate`
in [`openapi/openapi.yaml`](../../openapi/openapi.yaml).

## Who the agent can email

`send_email` reaches addresses you have on file, by name: your team members and
your saved contacts (`POST /v1/contacts`, MCP `create_contact`). A name shared by
two people, or not on file, sends nothing, and both get the same answer.

When a caller says "email me", the agent sends to the email saved for the caller's
number. It never falls back to a name. Caller ID picks that address, and caller ID
can be faked, so:

- Each email to the caller has a fixed subject start, first line and footer that
  say it was sent because of a phone call with your organization. They are in
  the call's language.
- If the agent uses the caller's saved name and it would reach someone else,
  nothing is sent.
- One call sends at most 3 emails.

The words `me` and `caller` (in email also `sender` and `you`) always mean the
person the agent talks to, never a contact with that name. In an email conversation, "email me"
goes to the sender, whose address is proven by DMARC. The agent never emails an address someone says or types. It answers
that it can only email addresses on file, to prevent spam.

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
- On a call to a [connected phone system](connect-phone-system.md), a contact
  can be an extension (`extension` on [`POST /contacts`](../../openapi/openapi.yaml)).
  Hail dials it through your phone system, and the person sees the caller's own number.
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

## Answer from your documents (knowledge base)

Give an agent your manuals, datasheets, FAQs and part lists. It answers calls, texts and email from them, in the caller's language.

```bash
# 1. Upload a file (CLI: one command does the upload link, the PUT and the add).
hail knowledge add northgate-ns5000-manual.pdf
# → ✓ Document added: 6f1c...  Status: pending

# 2. Wait until it is ready.
hail knowledge get 6f1c...
# → Status: ready  Pieces: 42

# 3. Turn the knowledge base on for an agent.
hail agents update <agent_id> --knowledge

# 4. Check what the agent will find.
hail knowledge search "what does error E-204 mean"
```

The knowledge base is in early access. It is off until Hail support turns it on for your workspace; until then adding documents, search and `knowledge: true` on an agent answer `403`. Listing, reading and deleting your documents always work.

### Add a document with the API

```bash
# 1. A one-time upload link (15 minutes, no key needed for the PUT).
curl -s "$HAIL_API_URL/v1/uploads" \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purpose": "knowledge_document", "filename": "manual.pdf", "content_type": "application/pdf"}'
# → {"id": "<upload_id>", "upload_url": "<api>/v1/uploads/<upload_id>?token=...", "max_bytes": 20971520, ...}

# 2. Send the bytes.
curl --silent --show-error --globoff --upload-file manual.pdf '<upload_url>'

# 3. Add it.
curl -s "$HAIL_API_URL/v1/knowledge/documents" \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"upload_id": "<upload_id>", "name": "NS-5000 manual"}'
# → {"id": "<document_id>", "status": "pending", ...}
```

Poll `GET /v1/knowledge/documents/{id}` until `status` is `ready` or `failed` (`error` says why). The full contract is in the [OpenAPI spec](../../openapi/openapi.yaml) under the `knowledge` tag: add, list, get, delete and search.

### Add a document from a browser

A web page can send the file straight to storage, without the bytes passing through the API. This flow suits [Uppy's `@uppy/aws-s3`](https://uppy.io/docs/aws-s3/) `signRequest`:

1. `POST /v1/knowledge/uploads` with `{"filename", "content_type", "size_bytes"}` reserves a storage `key` (15 minutes).
2. For each storage request, call `POST /v1/knowledge/uploads/{id}/sign` with `{"method", "s3_upload_id", "part_number"}`. It returns `{"url", "key", "headers"}`. Send the request to `url` with those `headers`. It signs a whole-file `PUT`, plus the multipart steps: start, part, list, finish, cancel. Each body must have its exact size: `size_bytes` for a whole-file `PUT`, 5 MiB per part (the last part takes the rest). In Uppy, set `getChunkSize` to `5 * 1024 * 1024`.
3. `POST /v1/knowledge/uploads/{id}/complete` checks the stored file's size, type and first bytes, then answers like `POST /v1/knowledge/documents`. A file that does not match is deleted (`422`).

`DELETE /v1/knowledge/uploads/{id}` cancels an upload. One never completed is deleted by itself after it expires. Self-hosting: list your web origins in `HAIL_ASSET_ALLOWED_ORIGINS`, then run `terragrunt apply`. This sets the bucket's CORS rules.

| Type     | `content_type`                                                            | Read as                               |
| -------- | ------------------------------------------------------------------------- | ------------------------------------- |
| PDF      | `application/pdf`                                                         | Text by page. Scans (no text) fail    |
| Word     | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | Paragraphs, headings and tables       |
| Text     | `text/plain`                                                              | Paragraphs                            |
| Markdown | `text/markdown`                                                           | Paragraphs, `#` headings              |
| CSV      | `text/csv`                                                                | One piece per row: `Header: value; …` |

Early-access limits per workspace: 10 documents, 20 MB per file, 1,000 pieces. One file gives at most 1,500 pieces whatever the workspace limit; split a larger one. A piece is about 300 to 500 words of text, or one CSV row. Ask Hail support for more.

### What the agent does with it

- **Calls**: when a call starts, Hail loads the workspace's pieces while the greeting plays. On each caller turn it finds the best 3 pieces and gives them to the model with that turn. Filler like "yes, thanks" is not searched, in calls, texts or email. The agent is told to use the pieces only when they answer the question.
- **Texts and email**: each reply gets the best 5 pieces for the message.
- **Search** works by meaning and by words together. A question in Italian finds an English manual. Part numbers and error codes match however they are spoken or typed: "NS BAT 48 V 100" finds "NS-BAT-48V-100".
- Document text is treated as reference data, never as instructions to the agent.
- Delete a document with `DELETE /v1/knowledge/documents/{id}` (or `hail knowledge delete <id>`): its file and pieces go at once.

In the [MCP server](mcp.md): `create_file_upload` (purpose `knowledge_document`), `add_knowledge_document`, `list_knowledge_documents`, `get_knowledge_document`, `delete_knowledge_document`, `search_knowledge`, and `knowledge` on `create_agent` / `update_agent`. In the Python SDK: `client.knowledge.add_document(...)`, `search(...)`.

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
`tools` list leaves them out. Each text names a team member, each email names a team member or
saved contact, and each email allows at most 3 sends.

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
