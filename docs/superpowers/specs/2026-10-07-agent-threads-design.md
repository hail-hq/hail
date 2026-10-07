# Agent threads: shared call and text history per caller

## Goal

An agent remembers one caller across calls and texts, in both directions.

- A text the caller sends during a call is known to the agent at once.
- A text sent after a call is known to the agent on the next call or text.
- The agent only sees history for the current caller number.
- If one number can only call, an org number with SMS fills the gap.

## Today (verified in code)

- Text agent context: last 20 texts, 24 hours, no call data (`core/hailhq/core/text_agent.py`, `build_chat_messages`).
- Voice agent context: no history (`core/hailhq/core/inbound_calls.py`, `open_inbound_call`).
- Call turns are `call_events` rows, kind `user_turn` or `agent_turn`, payload `{role, text}`.
- `sms.agent_id` is set only on inbound rows that queue an agent reply.
- `conversations` table exists, is client-supplied, and is not used by inbound. Not used here.
- Voice tools live in `core/hailhq/core/agent_tools/registry.py`. The text agent has no tools.
- `send_sms` tool: dialed number if it has SMS, else the org's oldest SMS number (`api/hailhq/api/routes/internal/agent.py:293`).

## Design

### 1. Thread = a query, no new table

A thread is every `calls` and `sms` row with:

- the same `organization_id`
- the same `agent_id`
- the same caller number: `from_e164` on inbound rows, `to_e164` on outbound rows

Changes:

- Set `sms.agent_id` on every agent-routed row, inbound and outbound. Today only inbound rows that queue a reply have it.
- Add two indexes per table (`sms`, `calls`), one per caller column (`from_e164`, `to_e164`), because the caller sits in a different column by direction.
- New `core` function `thread_items(org_id, agent_id, caller_e164, limit, before)`. It returns texts and call turns, ordered by time.
- Old calls appear with no backfill. Old texts appear only if they had `agent_id`.
- Calls and texts with no agent are not in any thread.

### 2. Auto-load at start

- Last 30 items from the last 7 days, rendered as plain text.
- Example: `[text in 10:02] ...` and `[call 10:05] caller: ... agent: ...`.
- A text longer than 500 characters is cut, with a note that `thread_history` has the full text.
- Voice: added to the system prompt when the call opens.
- Text agent: replaces `thread_messages`. Same 30 items, full text, no cut.

### 3. Tool `thread_history` (voice agent)

- Inputs: `before` (cursor), `limit`, `item_id` (full text of one item).
- No caller number input. The server finds the caller from the call.
- Registered in `agent_tools/registry.py`, filtered by `agent.tools` like the other tools.
- The text agent gets no tools. Out of scope.

### 4. Text during an active call

- Inbound text from the caller while that agent has an active call with the same caller:
  - The API pushes it to the voice session.
  - The voicebot adds it to the agent's conversation as: `Caller just texted: <body>`.
  - The text agent does not reply. The row is marked `agent_reply_state='skipped'`.

### 5. Sending number for `send_sms`

1. The dialed number, if it has SMS.
2. An org SMS number whose `sms_agent_id` is this agent.
3. An org SMS number whose `sms_agent_id` is empty. Set it to this agent.
4. None found: the agent tells the caller it cannot text. The tool stays listed while the org has any SMS number, and is hidden when the org has none.

- A number bound to another agent is never taken.
- Caller replies reach this agent. Both numbers feed the same thread.
- No SMS number in the org: no auto-buy, no shared pool number. The console shows a warning to add one.

### 6. Privacy and retention

- Every read is scoped by org, agent, and the caller number found by the server.
- No tool or API accepts a caller number to read.
- `retention.py` and `dsar.py` already cover `calls`, `sms` and `call_events`. No new table to add.

## Not included

- Email in threads.
- Threads for calls or texts with no agent.
- Backfill of old texts.
- Tools for the text agent.
- Taking an SMS number from another agent.

## Testing

- Unit: `thread_items` ordering, scope (other caller and other agent rows never returned), cut at 500 characters.
- Unit: sending number choice, all four steps.
- Integration: text during an active call reaches the voice session and skips the text reply.
- Integration: voice-only number plus SMS number. Call, text, call again. The second call prompt contains the text.
- Docs: update `docs/public/agents.md`.
