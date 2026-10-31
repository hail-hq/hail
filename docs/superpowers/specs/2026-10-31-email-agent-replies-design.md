# Email agent replies

**Date:** 2026-10-31

An organization can route an email address to an agent, the way a number routes
texts to one. The agent reads the thread and answers by email.

## Routing

- `email_domains.email_agent_id` names the agent. Null means no agent replies.
- `email_domains.email_agent_scope` is `members` (default) or `anyone`.
- `agents.email_enabled` (default true) says whether the agent answers email.
- `PATCH /email-domains/{id}` sets both fields. The agent must be live, in the
  organization, and have `email_enabled`.
- A custom domain must be verified with inbound on, or the PATCH returns 422. A
  hail-mail address always receives mail, so it does not need `inbound_enabled`:
  the agent route is its own switch.

## Who gets a reply

Inbound mail is stored as before. A new row becomes `agent_reply_state = 'pending'`
only when all of these hold:

1. The mail is not spam or a virus, and the org is under its inbound cap.
2. The domain has an agent that answers email.
3. It has no `Auto-Submitted` (other than `no`), `Precedence: bulk|list|junk`,
   `List-Id`, or `List-Unsubscribe` header.
4. It has an envelope sender. The `From` address is not
   `noreply`-style or a mailer daemon, and not an address on the hail-mail base
   domain, and not one of the message's own recipients.
5. The sender is proven, under both scopes: one well-formed `From` address and a
   DMARC `PASS`. Other mail gets no reply. A reply to a forged `From` would reach
   an innocent third party. SES reports `GRAY` when only an unaligned SPF or DKIM
   check passed, so `GRAY` does not prove the `From` address. A `From` header with
   defects, or whose angle-bracket address differs from the parsed one, is not
   proven: `alice@member.com <evil@attacker.com>` parses to alice.
6. Scope `members`: the sender is a verified login email of an org member.
7. The sender is under the thread cap: 20 agent sends in 24 hours, counting
   replies and tool sends in any channel. The cap is read when a run starts.
   Tool sends count as they go, but a run's own sends do not stop its one reply,
   and a reply that already exists is only delivered.
8. The org has funds.

Rows that fail get `agent_reply_state = 'skipped'` and `metadata.agent_skip_reason`,
on every path: ingest, a claim, expiry, the worker, and the reply route. A row
refused for its sender stays out of later chats.
With no funds, `suppressed_reasons` also gets `insufficient_funds`, the reason
the console already shows on inbound mail. The row stays stored and visible.

The rules run again before the model is called, funds included, and when the reply
is sent: routing, scope, funds, and the suppression gate can change while the model
writes.

## Replies

Tasks beside the voicebot's text worker claim email rows, so a slow email cannot
delay a text. They use the same lease, attempts, and backoff. The chat is the agent's instructions plus the thread: rows
linked by `In-Reply-To` and `References`, and only rows with this same sender, so
a forged `References` header cannot read another thread. Inbound rows join the
chat only with `emails.from_verified`, and not when they were spam, a virus, or
skipped for their sender. Ingest sets `from_verified` for one `From` address with
DMARC `PASS`, and nothing else sets it. An outbound row joins only if it answers
a row that qualifies. The newest rows win when a thread is long.

`POST /internal/agent/reply-email` sends one reply per inbound row. A retry reads
the earlier row: a sent one is done, and a failed one is delivered again, as is a
queued one past a 60-second grace. The reply carries
`In-Reply-To`, `References`, `Re:`, and `Auto-Submitted: auto-replied`. It goes
out from the hail-mail address, or `noreply@` on a custom domain: `To` and `Cc`
are sender-written, so they do not choose the `From`. The reply goes to `From`,
never `Reply-To`. SES
overrides `Message-ID`, so thread lookup also matches the local part of a
referenced id against `provider_message_id`. Replies bill as outbound email and
write audit action `agent.email.reply`.

## Tools

The owner chose full capability, with `anyone` as an explicit opt-in that states
the risk. Every run has a proven sender, so it gets every tool the agent allows
that works outside a call: `list_contacts`, `send_email`, and `send_sms`, each
aimed at a member by directory name. Call-only tools do not apply. Addresses never
reach the model, each reply may make at most 3 sends, and tool sends count toward
the thread cap. A send's id comes from its message: a retried run cannot repeat
it, and a different message to the same person still goes.

Email text is untrusted. The prompt says so, and scope `members` is the default.

## Tables checked

- `sms`: holds texts, not email. Its reply columns are copied onto `emails`,
  because the reply state belongs to the inbound row.
- `email_domains`: the closest table for routing, as `phone_numbers` is for texts.
- `agents`: holds `sms_enabled` and `voice_enabled`.
