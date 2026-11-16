---
target: "ChatGPT plugin directory (OpenAI)"
slug: chatgpt-plugin-directory
category: mcp-registry
url: "https://platform.openai.com/plugins"
score: 8.5
status: drafted
---

# ChatGPT plugin directory (OpenAI)

OpenAI lists MCP-backed plugins (earlier names: Apps, Connectors) in the ChatGPT directory. Users can already add Hail by hand. The guide is at <https://hail.so/mcp/chatgpt>.

## TODO

- [x] Plugin form started on the Plugins page
- [ ] Five positive and three negative test cases entered (see **Content**)
- [ ] Release notes entered
- [ ] Reviewer credentials entered in "Review details" (a test account with sample data; no MFA, no email or SMS codes, no magic links)
- [ ] Domain verification and the policy attestations completed
- [ ] Demo video recorded and its URL entered (the owner reported on 2026-11-16 that the video is still missing; the items above are not confirmed)
- [ ] Submitted
- [ ] Approved
- [ ] Published (a separate click after approval)

## Steps to submit

From <https://developers.openai.com/plugins/deploy/submission>, checked 2026-11-16.

1. Record a video. Show the test cases and the Hail tools working in ChatGPT.
2. Upload the video where a reviewer can open it without signing in (for example an unlisted YouTube video). OpenAI asks for a URL (`review.demo_recording_url`), not a file.
3. Open <https://platform.openai.com/plugins>. Open the Hail plugin.
4. Check every item in **TODO** above is done in the form: test cases, release notes, reviewer credentials, domain verification, policy attestations. Paste the video URL.
5. Click **Submit for review**.
6. Check progress under **Review status** on the Plugins page. OpenAI sends review feedback by email.
7. After approval, open the approved version and click **Publish plugin**.

## Content

An MCP review needs the items below. The test cases here are drafts. Check each one in ChatGPT before you enter it.

Positive test cases:

1. "Find call-capable Hail numbers in the UK and Canada and compare monthly fees." `tools_triggered`: `list_number_regions`, `quote_numbers`. `expected_behavior`: a table of offers with monthly and setup fees. No purchase.
2. "Text +1-415-555-0123 from my Hail number: the meeting moved to 3pm." `tools_triggered`: `list_numbers`, `send_sms`. `expected_behavior`: the text is sent and its status is shown.
3. "Email test@example.com that the report is ready, then tell me if it was delivered." `tools_triggered`: `send_email`, `get_email_events`. `expected_behavior`: the email is sent and its delivery events are listed.
4. "Show my last 5 calls and the transcript of the latest one." `tools_triggered`: `list_calls`, `get_events`. `expected_behavior`: a list of calls and the latest transcript.
5. "Which Hail account am I signed in to?" `tools_triggered`: `whoami`. `expected_behavior`: the account and organization name.

Negative test cases (Hail tools must not run):

1. "What is the capital of France?"
2. "Write me a poem about the sea."
3. "Translate 'good morning' into Spanish."

Other items:

- Release notes.
- Reviewer credentials, entered in the dashboard form "Review details". Never put them in the plugin package (the ZIP file uploaded on the Plugins page). OpenAI rejects a package that contains `test_credentials` or `reviewer_instructions`.
- `websiteURL`: `https://hail.so`
- `supportURL`: `https://hail.so/contact`
- `privacyPolicyURL`: `https://hail.so/legal/privacy`
- `termsOfServiceURL`: `https://hail.so/legal/terms`

## Notes

- The docs give no video length or format, and no review time.
- To appeal a rejection, reply to the rejection email. Include the plugin ID when you contact support.
- After publication, OpenAI scans the MCP server every day.
