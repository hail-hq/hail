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

## Current application

The owner is filling the draft. Submission is not confirmed.
The private submission tracker holds reviewer access and credit details. Keep credentials and promotion codes out of this repository.

- Server: `https://mcp.hail.so`, Streamable HTTP at the root path.
- Authentication: OAuth with Dynamic Client Registration. Public discovery precedes sign-in for tool execution.
- Documentation: <https://hail.so/docs/mcp>.
- Privacy policy: <https://hail.so/legal/privacy>.
- Tool names, arguments and annotations: the live MCP catalog and `mcp/hailhq/mcp/tools.py`.

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
