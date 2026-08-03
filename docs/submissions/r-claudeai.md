---
target: "r/ClaudeAI"
slug: r-claudeai
category: subreddit
url: "https://www.reddit.com/r/ClaudeAI/"
score: 7.6
status: drafted
---

# r/ClaudeAI

## TODO

- [ ] Confirm a Reddit account exists with enough age/karma to clear the subreddit's spam filter (brand-new accounts get auto-removed on this sub) — verify before submitting, don't assume.
- [ ] Re-read the live sidebar rules and the "Built with Claude" flair guidelines at submission time — I could not fetch reddit.com from this environment, so the checklist below is built from public secondary sources (search results, ClaudeLog), not the primary rules page. Confirm nothing has changed.
- [ ] Actually run the demo described in Content end-to-end (Claude.ai connected to a live or self-hosted Hail instance, one real outbound call via `place_call`, one real follow-up via `send_email`) and capture real output — a transcript excerpt and/or a short screen recording. Do not post the illustrative example below as if it were an already-captured result.
- [ ] Replace the illustrative transcript/prompt in Content with the real captured output from that run.
- [ ] Grab a fresh screen recording of the actual Claude session (connector call → `place_call` → `get_call` → `send_email`) — `hail/docs/assets/gifs/hail-tail-live-stream.gif` (in the separate `hail` repo, not `hail-website`) is a real product recording but shows generic `hail tail` output, not this specific Claude flow. Use it only as a fallback if a purpose-shot recording isn't ready in time.
- [ ] Decide whether to run the demo against Hail Cloud (`mcp.hail.so`) or self-hosted — affects a sentence in Content ("no keys to manage" vs. `HAIL_API_KEY`).
- [ ] Draft reviewed by a second pair of eyes for tone (no marketing-speak — this sub downvotes/removes posts that read like launch announcements).
- [ ] Submitted.
- [ ] Confirmed live (not auto-removed, flair applied correctly).

## Steps to submit

1. Log into the Reddit account you've decided to use (see TODO — check its age/karma first).
2. Go to `https://www.reddit.com/r/ClaudeAI/submit`.
3. Choose the **Text** post type (not Link, not Image/Video) — the body is copy that needs to render as formatted text with inline code blocks.
4. Paste the title from **Content → Title** into the title field.
5. Paste the full body from **Content → Post body** into the text field. Reddit's editor supports Markdown — the code fences will render as code blocks.
6. Click the flair picker (usually a tag/flag icon under the title field or a required prompt before you can submit) and select **Built with Claude**. If the sub requires flair before the submit button unlocks, do this before pasting the body so you don't lose the draft.
7. If the sub's markdown renderer mangles the fenced code blocks, fall back to Reddit's "Code Block" toolbar button and reformat those two snippets manually — don't submit with broken formatting.
8. Do **not** paste the GitHub link inline in the post body if the sub's automod flags outbound links from low-karma accounts. Instead, submit the post first, then immediately add the link as the **first top-level comment** (see Content → Suggested first comment) — this is the common workaround on demo-heavy subs and reads more natural anyway.
9. Preview the rendered post once before submitting — check the code blocks and confirm no stray Markdown broke.
10. Submit.
11. Post the first comment (link + any extra detail) within a minute or two of submission.
12. Check back within the hour: confirm the post wasn't auto-removed by AutoModerator (common triggers: too many links, too-new account, banned phrasing). If removed, message the mods with the removal reason before reposting — don't just resubmit blind.
13. Respond to comments for at least the first 24 hours — threads with an absent OP get down-ranked and read as drive-by promotion.

## Content

### Title

I gave Claude a real phone number and a real inbox (via MCP) — it called my no-show customers itself

### Flair

Built with Claude

### Post body

````markdown
**What I built:** a no-show follow-up agent for a small side-project booking system. When someone misses a slot, Claude decides whether to call or email them, does it, then reads back what happened and decides the next move — no branching logic I wrote by hand, just Claude driving tools.

**How:** I connected Claude.ai to [Hail](https://hail.so) — Settings → Connectors → Add custom connector → `https://mcp.hail.so` — and clicked Allow. That's the whole setup, no API keys pasted anywhere. Hail is a self-hostable, open-source (AGPLv3) comms server that exposes phone calls and email as MCP tools: `place_call`, `send_email`, `get_call`, `list_calls`, `get_email`, `list_emails`, `get_events`, plus a few reader tools for events/attachments/stats. I'm the person who built Hail, disclosing that up front — this post is about what Claude did with it, not a pitch.

**The actual prompt I gave it:**

> "Here's today's no-show list: [name, phone, missed slot time] x4. For each one, call and ask if they want to rebook, keep it under 30 seconds, don't be pushy. If you can't reach them or they ask for details, send a short follow-up email instead. Then tell me what happened with each."

Claude read the list, called `place_call` four times with a per-call `system_prompt` it wrote itself (varying tone slightly — brisker for the customer who'd already no-showed twice), polled `get_call` for status, and for the one call that went to voicemail, fell back to `send_email` on its own without me telling it to. It then summarized outcomes back to me in plain text — no formatting requested, it just did it.

**Snippet — one of the tool calls it made** (approximated from the MCP transcript):

​`json
{
  "tool": "place_call",
  "arguments": {
    "to": "+1415555xxxx",
    "system_prompt": "You're calling on behalf of [business]. Their 2pm slot today was missed. Ask, briefly and warmly, if they'd like to rebook this week. If they say no or don't answer clearly, thank them and end the call. Keep the whole call under 30 seconds."
  }
}
​`

**What actually happened on the calls:** [replace with the real transcript excerpt + outcome summary from the captured run before posting]

The part that stood out: I never told it "if voicemail, send an email" — that branch was Claude's own read of the situation, and it did it without asking me first. That's the bit that actually made me want to write this up instead of just quietly using it.

Repo's linked in the first comment if anyone wants to poke at it or self-host instead of using the hosted connector.
````

### Suggested first comment

```markdown
Repo, for anyone curious: https://github.com/hail-hq/hail (AGPLv3, self-hostable). MCP setup docs: docs/setup/mcp.md. The tool list Claude had access to here is `place_call`, `send_email`, `get_call`, `list_calls`, `get_email`, `list_emails`, `get_email_raw`, `get_email_attachment`, `get_events`, `get_email_events`, `get_email_stats` — eleven tools total on the server, I used a subset.

Happy to answer questions about the connector setup or what the call pipeline looks like under the hood.
```

### Assets

- Demo evidence (preferred): a fresh screen recording of this specific Claude session — capture before posting, no path exists yet.
- Fallback demo asset: `hail/docs/assets/gifs/hail-tail-live-stream.gif` (repo-relative, in the `hail` repo, not `hail-website`) — real product recording of `hail tail` streaming live call events. Generic, not specific to this Claude flow; use only if the purpose-shot recording isn't ready.
- Brand assets if a small inline mark is wanted (logo in a comment reply, not the post body): `hail-website/public/assets/hail-wordmark.svg`, `hail-website/public/assets/hail-wordmark-inverted.svg`, `hail-website/public/assets/monogram-512.png`. Do not lead the post with a logo — this sub penalizes anything that opens like an ad.

## Notes

- **SMS is not part of this post.** Hail's MCP server exposes eleven tools today, none of them SMS — outbound/inbound SMS (Twilio) is on the roadmap but unwired (see `hail` repo README milestones and `core/hailhq/core/providers/` — only `voice/twilio.py` and `email/ses.py` exist, no `sms/`). If asked in comments "does it text too?", answer "not yet, on the roadmap" — do not imply it already works.
- Voice calls are carried by Twilio under the hood; email is AWS SES (custom sender domains supported). Only mention these if a commenter asks "what's actually powering this" — the post itself is about Claude's behavior, not vendor plumbing.
- I could not verify the current r/ClaudeAI sidebar rules or exact "Built with Claude" flair requirements from a primary source in this environment (reddit.com fetches are blocked here). The post structure above (what/how/prompt/demo, disclosure of being the builder, link moved to first comment) follows the general convention reported for that flair via secondary sources — re-verify against the live rules before submitting.
- Self-promotion risk: I built Hail, so disclose that plainly in the post (done above) rather than posing as a neutral user — this sub and most Claude-adjacent subs remove non-disclosed vendor posts on sight if discovered later, and it's worse for the account than a slow first post.
- Engagement expectation: reply to every top-level comment for the first day; a silent OP on a "built with Claude" post reads as drive-by marketing even with disclosure.
- If the demo run surfaces a genuinely interesting failure (e.g., Claude mishandles an ambiguous voicemail greeting, or over-explains on a call it was told to keep brief), consider leading the post with that instead — this sub responds better to "here's where it broke and what I learned" than a clean success story.
