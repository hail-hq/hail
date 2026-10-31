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

## Who gets a reply

Inbound mail is stored as before. A new row becomes `agent_reply_state = 'pending'`
only when all of these hold:

1. The mail is not spam or a virus, and the org is under its inbound cap.
2. The domain has an agent that answers email.
3. It has no `Auto-Submitted` (other than `no`), `Precedence: bulk|list|junk`,
   `List-Id`, or `List-Unsubscribe` header.
4. It has one `From` address and an envelope sender. That address is not
   `noreply`-style or a mailer daemon, and not an address on the hail-mail base
   domain, and not one of the message's own recipients.
5. Scope `members`: the sender is a verified login email of an org member, and
   the DMARC verdict is `PASS`. Any other verdict gets no reply. SES reports
   `GRAY` when only an unaligned SPF or DKIM check passed, so `GRAY` does not
   prove the `From` address.
6. Scope `anyone`: the DMARC verdict is not `FAIL`, so a forged `From` does not
   turn a reply into mail to a third party.
7. The sender is under the thread cap (20 replies in 24 hours).
8. The org has funds.

Rows that fail get `agent_reply_state = 'skipped'` and `metadata.agent_skip_reason`.
With no funds, `suppressed_reasons` also gets `insufficient_funds`, the reason
the console already shows on inbound mail. The row stays stored and visible.

The rules run again when the reply is sent: routing, scope, funds, and the
suppression gate can change while the model writes.

## Replies

The voicebot's text worker also claims email rows, with the same lease, attempts,
and backoff as texts. The chat is the agent's instructions plus the thread: rows
linked by `In-Reply-To` and `References`, and only rows with this same sender, so
a forged `References` header cannot read another thread.

`POST /internal/agent/reply-email` sends one reply per inbound row, from the
address the mail reached, with `In-Reply-To`, `References`, `Re:`, and
`Auto-Submitted: auto-replied`. The reply goes to `From`, never `Reply-To`. SES
overrides `Message-ID`, so thread lookup also matches the local part of a
referenced id against `provider_message_id`. Replies bill as outbound email and
write audit action `agent.email.reply`.

## Tools

The owner chose full capability. An email run gets every tool the agent allows
that works outside a call: `list_contacts`, `send_email`, and `send_sms`, each
aimed at a member by directory name. Call-only tools do not apply. Addresses
never reach the model, and each reply may make at most 3 sends. A retried run
does not repeat a send to the same person.

Email text is untrusted. The prompt says so, and scope `members` is the default.

## Tables checked

- `sms`: holds texts, not email. Its reply columns are copied onto `emails`,
  because the reply state belongs to the inbound row.
- `email_domains`: the closest table for routing, as `phone_numbers` is for texts.
- `agents`: holds `sms_enabled` and `voice_enabled`.
