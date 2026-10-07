# Agent threads: shared call and text history per caller

## Goal

An agent remembers one caller across calls and texts, in both directions.

- A text the caller sends during a call is known to the agent at once.
- A text sent after a call is known to the agent on the next call or text.
- The agent only sees history for the current caller number.
- If one number can only call, an org number with SMS fills the gap.

## Today (verified in code)

- Text agent context: last 20 texts, 24 hours, no call data (`core/hailhq/core/text_agent.py`, `build_chat_messages`). Kept, plus the `thread_history` tool.
- Voice agent context: no history (`core/hailhq/core/inbound_calls.py`, `open_inbound_call`). Kept, plus the `thread_history` tool.
- Call turns are `call_events` rows, kind `user_turn` or `agent_turn`, payload `{role, text}`.
- `sms.agent_id` is set only on inbound rows that queue an agent reply.
- `conversations` table exists, is client-supplied, and is not used by inbound. Not used here.
- Voice tools live in `core/hailhq/core/agent_tools/registry.py`. The text agent had no tools; it now gets `thread_history` only.
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
- New `core` function `thread_items(org_id, agent_id, caller_e164, limit, before, source, window)`. It returns texts and/or call turns, ordered by time.
- `active_call_for_thread` only counts dialing, ringing or in-progress calls created in the last 2 hours, so one stuck row cannot silence the text agent.
- Old calls appear with no backfill. Old texts appear only if they had `agent_id`.
- Calls and texts with no agent are not in any thread.

### 2. No history in prompts

- Prompts carry no history. An earlier version loaded the last 30 items into the voice prompt. Logfire and DB evidence: the prompt held the caller's texts, yet the agent said "I don't have that number". The record also held the agent's own earlier "I don't have it" call lines, which the model repeated (2 of 5 right with them, 12 of 12 once they were gone).
- Voice: when the call's built tools include `thread_history`, one line at the end of the instructions tells the agent to use it before it says it has no record (`THREAD_TOOL_HINT_VOICE` in `core/hailhq/core/prompts.py`). The call looks up its `ThreadScope` once at start and hands it to the tools.
- Text agent: chat = last 20 texts of the last 24 hours (inbound `user`, outbound `assistant`), plus agent-less `sms` rows between the receiving number and the caller, current text last. No call turns. The system prompt says to use `thread_history` (`THREAD_TOOL_HINT_TEXT`).
- Rendered lines: `[2026-10-06 10:02 UTC] text from caller: ...` and `[2026-10-06 10:05 UTC] on a call, caller: ...`. A text longer than 500 characters is cut, with a note that `item_id` returns the full text.

### 3. Tool `thread_history` (both agents)

- Inputs: `source` (`sms`, `voice`, `all`; anything else reads `all`), `before` (cursor), `limit`, `item_id` (full text of one item).
- No caller number input. The scope is `ToolContext.thread` (`ThreadScope`: org, agent, caller, org number), set by the server from the call or the inbound text; without it, the call on `ToolContext.call_id`. The org must match the run's org.
- The source filter runs in SQL, so `limit` and `before` stay exact.
- Voice: registered in `agent_tools/registry.py`, filtered by `agent.tools` like the other tools.
- Text agent: its only tool, at most 3 model calls per reply; a failed tool call reads as a short apology.

### 4. Text during an active call

- Inbound text from the caller while that agent has an active call with the same caller:
  - No API push. The voicebot polls the thread every 2 seconds (`voicebot/hailhq/voicebot/text_watch.py`).
  - It adds the text to the agent's conversation with the prefix `[text message from caller] `, cut at 1000 characters.
  - Delivery is at-least-once: a rare duplicate is possible.
  - The text agent does not reply. The row is marked `agent_reply_state='skipped'`.
  - Ingest also sets `metadata.skipped_reason = "active_call"` on that row. The watcher injects only rows still `skipped` with that marker, and the requeue is one conditional UPDATE on both; skips for other reasons (expiry, routing change) are never revived. The watch window starts 30 seconds before the call row's `created_at`, so texts that arrive while an outbound call rings count. A delivered text is set to `done` with `metadata.delivered_to_call`, so later calls never see it. A text whose injection fails 3 times stays `skipped` and is revived at call end.
  - `requeue_skipped_for_call` (core) revives undelivered texts: inbound texts of the thread still `skipped` with the marker, from 30 seconds before the call row, become `pending` with `metadata.requeued_at` set in place of the marker. The reply age limit counts from `requeued_at`; `requested_at` keeps the arrival time, so revived texts keep their order. One conditional UPDATE. It runs at the end of the voicebot's `on_call_end` (every end path) and in `sweep_stale_calls` for each call it force-closes.

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
- History the agent reads with the tool goes to the agent's LLM, including a bring-your-own endpoint.
- `retention.py` and `dsar.py` already cover `calls`, `sms` and `call_events`. No new table to add.

## Not included

- Email in threads.
- Threads for calls or texts with no agent.
- Backfill of old texts.
- Tools for the text agent other than `thread_history`.
- Taking an SMS number from another agent.

## Testing

- Unit: `thread_items` ordering, scope (other caller and other agent rows never returned), cut at 500 characters.
- Unit: sending number choice, all four steps.
- Integration: text during an active call reaches the voice session and skips the text reply.
- Integration: voice-only number plus SMS number. Call, text, call again. On the second call `thread_history` (source `sms`) returns the text; the prompt holds no history.
- Docs: update `docs/public/agents.md`.
