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
- [ ] Reviewer credentials entered in "Review details"
- [ ] Demo video recorded and its URL entered (the owner reports this is the last missing item, as of 2026-11-16)
- [ ] Submitted
- [ ] Approved
- [ ] Published (a separate click after approval)

## Steps to submit

From <https://developers.openai.com/plugins/deploy/submission>, checked 2026-11-16.

1. Record a video. Show the test cases and the Hail tools working in ChatGPT.
2. Upload the video where a reviewer can open it without signing in (for example an unlisted YouTube video). OpenAI asks for a URL (`review.demo_recording_url`), not a file.
3. Open <https://platform.openai.com/plugins>. Open the Hail plugin.
4. Paste the video URL in the review details. Submit.
5. Check progress under **Review status** on the Plugins page. OpenAI sends review feedback by email.
6. After approval, open the approved version and click **Publish plugin**.

## Content

An MCP review needs:

- Five positive test cases (each with `tools_triggered` and `expected_behavior`) and three negative cases.
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
