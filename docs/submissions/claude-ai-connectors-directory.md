---
target: "Claude.ai Connectors Directory"
slug: claude-ai-connectors-directory
category: mcp-registry
url: "https://claude.ai/directory/manage"
score: 5
status: drafted
---

# Claude.ai Connectors Directory

Review the live [Directory Policy](https://support.claude.com/en/articles/13145358-anthropic-software-directory-policy) before submitting.

## Listing content

Paste these into the portal at <https://claude.ai/directory/manage>.

- **Name:** Hail
- **Tagline (55 characters or fewer):** Phone, SMS and email for agents.
- **Categories:** Communication, Productivity, Developer Tools
- **Docs URL:** <https://hail.so/docs/mcp>
- **Privacy policy URL:** <https://hail.so/legal/privacy>
- **Support contact:** hi@hail.so
- **Server:** `https://mcp.hail.so`, Streamable HTTP at the root path
- **Authentication:** OAuth with Dynamic Client Registration. Public discovery precedes sign-in for tool execution.
- **Icon:** the square monogram from the hail-website repo, `public/assets/monogram-512.png`.

**Description (2,000 characters or fewer):**

> Hail gives an AI agent a phone number, an inbox and a voice. One remote MCP server covers phone calls, text messages and email.
>
> Place an outbound AI call and read its transcript. Send and receive text messages. Send email, read replies and attachments, and check delivery events and stats. Buy and route phone numbers, manage saved agents, and submit carrier registrations.
>
> Sign in with OAuth. Each person authorizes their own Hail account. Hail is open source (AGPLv3) and can be self-hosted. The same features are available in the CLI, the Python SDK and the OpenAPI spec.

**Use cases:**

- Place an outbound AI call and read the outcome.
- Send an email, then check whether it was delivered, opened or bounced.
- Send a text message and read the replies.
- Buy a phone number and route it to a saved agent.

**Prerequisites:** a Hail account with credit. Calls and texts need an active phone number. Email needs a verified sending domain.

**Data direction:** read and write.

**Tools:** the portal syncs the live list. The server has 43 tools, listed in `mcp/hailhq/mcp/tools.py`.

| Group                | Tools                                                                                                                                                                                           |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Calls                | `place_call`, `get_call`, `list_calls`, `get_events`                                                                                                                                            |
| Text messages        | `send_sms`, `get_sms`, `list_sms`, `preview_sms_application`, `get_sms_application`, `amend_sms_application`                                                                                    |
| Email                | `send_email`, `upload_email_attachment`, `create_file_upload`, `get_email`, `list_emails`, `get_email_raw`, `get_email_attachment`, `get_email_events`, `get_email_stats`, `list_email_domains` |
| Agents               | `create_agent`, `update_agent`, `get_agent`, `list_agents`, `delete_agent`, `route_number`                                                                                                      |
| Numbers              | `list_number_regions`, `quote_numbers`, `acquire_number`, `list_numbers`, `get_number`, `release_number`                                                                                        |
| Registrations        | `get_verification_requirements`, `check_verification`, `submit_verification`, `confirm_verification`, `get_verification`, `list_verifications`, `cancel_verification`                           |
| Contacts and account | `create_contact`, `list_contacts`, `lookup_contact`, `whoami`                                                                                                                                   |

## Portal steps

1. Introduction: read the overview.
2. Connection: server URL `https://mcp.hail.so`, transport HTTP (Streamable), per-user OAuth.
3. Tools: the portal syncs the live tools and checks `title` and the read-only and destructive hints.
4. Listing and use cases: paste the fields above.
5. Company: use the legal name and contact on <https://hail.so/legal>.
6. Authentication: OAuth. Discovery is at `https://mcp.hail.so/.well-known/oauth-protected-resource`.
7. Data handling: first-party API.
8. Test and launch: paste reviewer access (see Reviewer testing below).
9. Compliance: check the acknowledgements below only after reading each against current practice.
10. Review: fix what the checklist flags, then submit.

Escalate a stuck review to `mcp-review@anthropic.com`.

## Policy acknowledgements

1. The submitter must read the guidelines personally before affirming that statement.
2. Hail's MCP server calls Hail's own API. The API operates the communication and registration services.
3. Tools charge for Hail services, including number purchases. They do not transfer money or financial assets between users.
4. **AI audio needs written permission.** Hail's live AI calls use text-to-speech. Section 4.B includes voice models and text-to-speech. The current connector cannot truthfully affirm that it generates no AI audio.
5. Descriptions must state tool contracts without model instructions or directions to invoke other tools. Runtime consent, confirmation and authorization checks remain necessary.
6. The MCP interface receives tool arguments, not Claude's full chat history. MCP diagnostic exports exclude request bodies, tool content, model messages and transcripts. Functional call, text, email and registration records remain subject to the privacy policy.
7. Public documentation must remain available before publication.

Code changes must be merged, deployed and verified before they support a live-server acknowledgement.
The code review is not a legal or organization-wide privacy certification.

## Reviewer testing

The policy requires a review account with sample data and at least three working examples.
A credit code alone does not supply a populated review account.
Reviewer instructions must include account access, OAuth steps, available senders and test destinations.

The self-tested statement requires actual tool execution through MCP Inspector or a custom Claude connector.
Unit tests, tool discovery and calls through other clients are separate evidence.
Tests that require paid actions, number release or registration documents need explicit authorization and disposable resources.
Each tool needs a recorded result. An error response alone does not demonstrate successful functionality.

## Remaining steps

- Obtain written permission for AI phone calls, or agree a separate connector scope with Anthropic.
- Deploy and verify the description and MCP telemetry changes.
- Prepare the review account and complete the client-level tool tests.
- Complete the truthful acknowledgements and submit the draft.
