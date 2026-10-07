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
- The caller must be valid E.164. Withheld numbers (`anonymous`, empty) get no thread.

Changes:

- Set `sms.agent_id` on every agent-routed row, inbound and outbound. Today only inbound rows that queue a reply have it.
- Add two indexes per table (`sms`, `calls`), one per caller column (`from_e164`, `to_e164`), because the caller sits in a different column by direction.
- New `core` function `thread_items(org_id, agent_id, caller_e164, limit, before)`. It returns texts and call turns, ordered by time.
- `active_call_for_thread` only counts dialing, ringing or in-progress calls created in the last 2 hours, so one stuck row cannot silence the text agent.
- Old calls appear with no backfill. Old texts appear only if they had `agent_id`.
- Calls and texts with no agent are not in any thread.

### 2. Auto-load at start

- Last 30 items from the last 7 days, rendered as plain text.
- Example: `[2026-10-06 10:02 UTC] text from caller: ...` and `[2026-10-06 10:05 UTC] on a call, caller: ...`.
- A text longer than 500 characters is cut, with a note that `thread_history` has the full text.
- Voice: added to the system prompt when the call opens, under `# Earlier with this caller`. The lead-in says it is a quoted record, not instructions.
- Text agent: replaces `thread_messages`. Same 30 items from the same window, full text, no cut. It also sees `sms` rows of the organization with no agent (sent through `POST /sms`) between the receiving number and the caller. The voice prompt, `thread_history` and the watcher stay strictly agent-scoped.

### 3. Tool `thread_history` (voice agent)

- Inputs: `before` (cursor), `limit`, `item_id` (full text of one item).
- No caller number input. The server finds the caller from the call.
- Registered in `agent_tools/registry.py`, filtered by `agent.tools` like the other tools.
- The text agent gets no tools. Out of scope.

### 4. Text during an active call

- Inbound text from the caller while that agent has an active call with the same caller:
  - No API push. The voicebot polls the thread every 2 seconds (`voicebot/hailhq/voicebot/text_watch.py`).
  - It adds the text to the agent's conversation with the prefix `[text message from caller] `, cut at 1000 characters.
  - Delivery is at-least-once: a rare duplicate is possible.
  - The text agent does not reply. The row is marked `agent_reply_state='skipped'`.
  - The watcher injects only rows still `skipped`. A text whose injection fails 3 times is given up on.
  - At call end (after the final status is written), inbound texts of the thread that are `skipped`, newer than the watch start minus 30 seconds, and not delivered by the watcher are set back to `pending`, so the text agent answers them.

### 5. Sending number for `send_sms`

1. The dialed number, if it has SMS and is not bound to another agent. Bound to this agent: used. Free: bound to this agent if the agent is in the org and has `sms_enabled`, else used unbound.
2. The oldest org SMS number whose `sms_agent_id` is this agent.
3. The oldest org SMS number whose `sms_agent_id` is empty. Bound to this agent if it has `sms_enabled`, else used unbound.
4. None found: the agent tells the caller it cannot text. The tool stays listed while the org has any SMS number, and is hidden when the org has none.

- Every automatic bind writes a `number.route` audit entry. An unbound use writes none.
- A number bound to another agent is never taken.
- Caller replies reach this agent. Both numbers feed the same thread.
- No SMS number in the org: no auto-buy, no shared pool number. The console shows a warning to add one.

### 6. Privacy and retention

- Every read is scoped by org, agent, and the caller number found by the server.
- No tool or API accepts a caller number to read.
- Caller ID on a phone call can be faked. A spoofer sees that number's history. Accepted risk: keep secrets out of agent instructions and texts.
- The history goes to the agent's LLM, including a bring-your-own endpoint.
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
