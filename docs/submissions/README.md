# Submission tracker

## Listing copy

Decided 2026-11-16. Paste these into every listing. Each submission file below already holds the right version for its fields.

Rules:

- Sell the outcome: the AI gets its own phone number and email inbox.
- Describe the work broadly ("handles calls, texts, and email"). Do not list narrow verbs ("make calls, send texts"): they read as limits.
- Always write "email inbox", never "inbox" alone. Non-native English readers may not know the word. `validate_submission.py` fails on a bare "inbox".
- Use one wording everywhere. Do not A/B test across directories: traffic per listing is too small to compare.
- Each pasted copy in a submission file sits between `<!-- copy:NAME -->` and `<!-- /copy -->`. CI fails when it differs from the block with the same name below. To change the wording, edit the block here, then the marked copies. The hail.so brand page shows the same blocks with copy buttons (`lib/brand-copy.ts` in the website repo). Change it in the same round.

**Tagline** (52 characters; fits Claude's 55 and Product Hunt's 60. Use "real phone number" when it fits):

<!-- copy:tagline -->

> Give your AI a real phone number and an email inbox.

<!-- /copy -->

**Short** (96 characters; fits the 100-character MCP Registry `description`):

<!-- copy:short -->

> Run your AI assistant with real phone numbers and email inboxes. Handle calls, texts, and email.

<!-- /copy -->

**Medium** (253 characters; fits Product Hunt's 260 and card descriptions):

<!-- copy:medium -->

> Give your AI assistant a real phone number and an email inbox. Run a support receptionist, answering service, or your assistant on one account. Get a number in 120+ countries and email on your own domain, with MCP, API, and webhook support for your app.

<!-- /copy -->

**Speed line** (for the GitHub repo description and the `README.md` header; the owner wants it tried as a strong second line in more places, such as a hail.so section heading or the footer):

<!-- copy:speed -->

> Give your agent a real phone number and an email inbox, in minutes, not days.

<!-- /copy -->

**Long** (681 characters; for Claude, Muse, ChatGPT and other long description fields):

<!-- copy:long -->

> Hail gives your AI assistant a real phone number and an email inbox. Your agent can do any work people do on the phone and in email.
>
> Run a support receptionist, answering service, or your own assistant on one account. It handles calls, texts, and email day and night, and passes calls to your phone when needed.
>
> Get a local or toll-free number in 120+ countries, even where you have no office. Hail helps with the paperwork some countries need. Send and receive email on your own domain.
>
> Webhooks send every call, text, and email to your app, so your own tools stay up to date.
>
> Connect Claude, ChatGPT, or Cursor over MCP in minutes, or build with the API. Hail is open source.

<!-- /copy -->

Search terms in this copy, with US monthly search volume (Ahrefs, October and November 2026): ai receptionist (13,000), ai answering service (3,900), ai phone agent (1,700), phone number for ai agent (20).

## Tracker

Ranked by relevance/impact score, highest first. Update the **Status** column as work progresses: `drafted` -> `submitted` -> `submitted (live)` or `rejected` / `n/a`.

| Rank | Target                                             | Category      | Score | Status                                                                                                                        | File                                                                                                         |
| ---- | -------------------------------------------------- | ------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 1    | awesome-mcp-servers (punkpeye/awesome-mcp-servers) | github-list   | 9.5   | submitted ([PR #17107](https://github.com/punkpeye/awesome-mcp-servers/pull/17107), open since 2026-10-20; PR #9561 closed) | [awesome-mcp-servers-punkpeye-awesome-mcp-servers.md](./awesome-mcp-servers-punkpeye-awesome-mcp-servers.md) |
| 2    | Official MCP Registry (modelcontextprotocol.io)    | mcp-registry  | 9.3   | submitted (live: [io.github.hail-hq/hail-mcp v0.1.3](https://registry.modelcontextprotocol.io/v0.1/servers?search=hail-mcp)) | [official-mcp-registry-modelcontextprotocol-io.md](./official-mcp-registry-modelcontextprotocol-io.md)       |
| 3    | PulseMCP                                           | mcp-registry  | 9     | waiting (auto-ingests from Official Registry, published 2026-07-07 — check back ~2026-07-14)                                  | [pulsemcp.md](./pulsemcp.md)                                                                                 |
| 4    | Postman API Network                                | dev-directory | 9     | submitted (live: [hail-hq/hail-api](https://www.postman.com/hail-hq/hail-api), auto-sync on)                                  | [postman-api-network.md](./postman-api-network.md)                                                           |
| 5    | ChatGPT plugin directory (OpenAI)                  | mcp-registry  | 8.5   | drafted (form started; video URL and review items open)                                                                       | [chatgpt-plugin-directory.md](./chatgpt-plugin-directory.md)                                                 |
| 6    | Smithery                                           | mcp-registry  | 8     | n/a (now an Arcade directory; old submit path shut down 2026-10-31)                                                           | [smithery.md](./smithery.md)                                                                                 |
| 7    | Cursor Marketplace                                 | mcp-registry  | 8     | drafted (plugin ready; submit at [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish))                    | [cursor-marketplace.md](./cursor-marketplace.md)                                                             |
| 8    | r/mcp                                              | subreddit     | 7.8   | drafted                                                                                                                       | [r-mcp.md](./r-mcp.md)                                                                                       |
| 9    | r/ClaudeAI                                         | subreddit     | 7.6   | drafted                                                                                                                       | [r-claudeai.md](./r-claudeai.md)                                                                             |
| 10   | r/SideProject                                      | subreddit     | 7.4   | drafted                                                                                                                       | [r-sideproject.md](./r-sideproject.md)                                                                       |
| 11   | Product Hunt                                       | dev-directory | 7.2   | drafted (HOLD)                                                                                                                | [product-hunt.md](./product-hunt.md)                                                                         |
| 12   | Hacker News – Show HN                              | dev-directory | 7     | drafted                                                                                                                       | [hacker-news-show-hn.md](./hacker-news-show-hn.md)                                                           |
| 13   | Muse Connector Platform (Meta)                     | mcp-registry  | 7     | drafted (guidelines checked 2026-11-18; test account open) | [muse-connector-platform.md](./muse-connector-platform.md)                                                   |
| 13   | Hermes Agent MCP catalog (Nous Research)           | mcp-registry  | 7     | submitted ([PR #157513](https://github.com/NousResearch/hermes-agent/pull/157513), 2026-11-18) | [hermes-mcp-catalog.md](./hermes-mcp-catalog.md) |
| 13   | ClawHub (OpenClaw skills registry)                 | mcp-registry  | 7     | submitted (live: [hail-hq/hail](https://clawhub.ai/hail-hq/skills/hail) and [r13i/hail](https://clawhub.ai/r13i/skills/hail)) | [clawhub.md](./clawhub.md) |
| 14   | Glama MCP Registry                                 | mcp-registry  | 6.8   | n/a — abandoned (stdio/mcp-proxy architecture mismatch)                                                                       | [glama-mcp-registry.md](./glama-mcp-registry.md)                                                             |
| 15   | mcp.so                                             | mcp-registry  | 6.5   | submitted ([issue #7604](https://github.com/chatmcp/mcpso/issues/7604), 2026-11-18) | [mcp-so.md](./mcp-so.md)                                                                                     |
| 16   | r/AI_Agents                                        | subreddit     | 6.3   | drafted                                                                                                                       | [r-ai-agents.md](./r-ai-agents.md)                                                                           |
| 17   | Nango (NangoHQ/nango providers.yaml)               | github-list   | 6.2   | drafted                                                                                                                       | [nango.md](./nango.md)                                                                                       |
| 18   | r/opensource                                       | subreddit     | 6     | drafted                                                                                                                       | [r-opensource.md](./r-opensource.md)                                                                         |
| 19   | r/selfhosted                                       | subreddit     | 5.9   | drafted                                                                                                                       | [r-selfhosted.md](./r-selfhosted.md)                                                                         |
| 20   | Future Tools (futuretools.io)                      | ai-directory  | 5.8   | drafted                                                                                                                       | [future-tools-futuretools-io.md](./future-tools-futuretools-io.md)                                           |
| 21   | awesome-selfhosted (awesome-selfhosted-data)       | github-list   | 5.5   | drafted                                                                                                                       | [awesome-selfhosted-awesome-selfhosted-data.md](./awesome-selfhosted-awesome-selfhosted-data.md)             |
| 22   | AlternativeTo                                      | dev-directory | 5     | drafted                                                                                                                       | [alternativeto.md](./alternativeto.md)                                                                       |
| 23   | Claude.ai Connectors Directory                     | mcp-registry  | 5     | submitted (in review, owner 2026-11-16: [claude.ai/directory/manage](https://claude.ai/directory/manage))                     | [claude-ai-connectors-directory.md](./claude-ai-connectors-directory.md)                                     |
| 24   | There's An AI For That (TAAFT)                     | ai-directory  | 4.8   | drafted                                                                                                                       | [there-s-an-ai-for-that-taaft.md](./there-s-an-ai-for-that-taaft.md)                                         |
| 25   | Toolify.ai                                         | ai-directory  | 4.3   | drafted                                                                                                                       | [toolify-ai.md](./toolify-ai.md)                                                                             |
| 26   | Cursor directory (community)                       | mcp-registry  | 4     | drafted (listing not checked: site rate-limits scripts)                                                                       | [cursor-directory-community.md](./cursor-directory-community.md)                                             |
| 27   | OpenTools                                          | mcp-registry  | 3.8   | drafted (not listed 2026-11-16; no submit path found)                                                                         | [opentools.md](./opentools.md)                                                                               |
| 28   | Futurepedia                                        | ai-directory  | 3.5   | drafted                                                                                                                       | [futurepedia.md](./futurepedia.md)                                                                           |
| 29   | r/LocalLLaMA                                       | subreddit     | 2     | drafted                                                                                                                       | [r-localllama.md](./r-localllama.md)                                                                         |
| 30   | Grok custom connectors (xAI)                       | mcp-registry  | 0     | n/a (no public directory; users add the URL at grok.com/connectors)                                                           | [grok-custom-connectors.md](./grok-custom-connectors.md)                                                     |

## TODO

- [ ] **Reconcile the duplicate tracker in `hail-website/submissions/`.** A
      second, divergent copy exists at `/Users/r/playground/hail-website/submissions/`
      (9 files, dated 2026-07-14 — newer than these). Both trees are untracked in
      git, so neither has a committed history to arbitrate. **This directory is
      authoritative** (decided 2026-07-15): it holds the tracker, `_TEMPLATE.md`,
      and `validate_submission.py`, and matches `2026-07-06-registry-submissions-design.md`,
      which specifies `docs/submissions/<slug>.md` in the `hail` repo. - Three files exist in both and **their contents differ**: `mcp-so.md`,
      `toolify-ai.md`, `r-claudeai.md`. The newer copies may contain real work. - Six exist only in `hail-website/submissions/`, under shorter slugs — same
      targets, different filenames: `awesome-mcp-servers-punkpeye.md` (here:
      `awesome-mcp-servers-punkpeye-awesome-mcp-servers.md`),
      `awesome-mcp-servers-appcypher.md`, `awesome-mcp-servers-wong2.md`,
      `awesome-voice-agents-yzfly.md`, `awesome-ai-agents-e2b.md`,
      `mcp-registry.md` (here: `official-mcp-registry-modelcontextprotocol-io.md`). - Merge anything of value into this tree, add the missing targets to the
      table above, then delete `hail-website/submissions/`.
- [x] **Re-check `awesome-selfhosted-awesome-selfhosted-data.md`'s copy before
      submitting.** Refreshed 2026-08-23: SMS added, platforms trimmed to
      Python + Docker, submit date 2026-09-01. Other drafts of the 2026-07-07
      vintage may still be stale for the same reason.
