---
target: "Muse Connector Platform (Meta)"
slug: muse-connector-platform
category: mcp-registry
url: "https://muse.ai/platform"
score: 7
status: drafted
---

# Muse Connector Platform (Meta)

Muse is Meta's assistant. Users can already add Hail without a listing: they ask Muse in a chat to create a Custom Connector. The guide will be at <https://hail.so/mcp/muse> once it ships. A listing puts Hail in the Muse connector list.

## TODO

- [x] Re-checked against <https://platform.muse.ai/docs> on 2026-11-18 ("Connector guidelines")
- [x] The guide <https://hail.so/mcp/muse> is live
- [x] Tool list with Read, Write, and Sensitive classes (below), as the guidelines ask
- [x] Reviewer test account created 2026-11-18 (see **Notes**)
- [ ] Business verification through Persona (company documents of the legal entity on <https://hail.so/legal>)
- [ ] Data processing questionnaire answered
- [ ] Reviewer sign-in entered in the Muse portal
- [ ] Sign-in test passes in the portal (Technical details, Test)
- [ ] Submitted
- [ ] Confirmed live

## Steps to submit

The order (describe, submit for review, appear in the directory) and the required information come from Muse's own guidelines at <https://platform.muse.ai/docs>, read 2026-11-18. The form itself is behind a sign-in, so the screen names in steps 2 to 5 come from a third-party walkthrough (Manufact, updated 2026-11-04) and are not verified.

1. Open <https://platform.muse.ai/login?return_to=%2Fsubmission>. Sign in or create an account with your work email.
2. **Overview**: fill the fields from **Content** below. Upload the icon. Answer "No" to accepting payments inside Muse.
3. **Technical specs**: set **Connection type** to **Existing MCP**. Enter the **Hosted MCP endpoint** and the docs URL. Set **Authentication methods** to **OAuth with PKCE**.
4. **Review**: check the boxes, agree to the Muse Connector Terms, click **Submit for review**.
5. Open the developer portal at <https://platform.muse.ai/>. Complete the three requirements:
   1. **Business verification**: click **View**, then finish the Persona check.
   2. **Data processing questionnaire**: answer every section. Use the privacy policy and the DPA at <https://hail.so/legal>.
   3. **Technical details**: confirm the endpoint and OAuth. Add the reviewer test account. Choose **Set up sign-in test**, then **Sign in to test**. Check the **MCP listing** view shows the Hail tools with titles and hints.
6. Record the date and status in **Notes**.

## Content

- **Connector name:** Hail
- **Company or developer:** the legal entity name on <https://hail.so/legal>
- **Product website:** `https://hail.so`
- **Hosted MCP endpoint:** `https://mcp.hail.so`
- **API or MCP documentation:** `https://hail.so/docs/mcp`
- **Support email:** `hi@hail.so`
- **Privacy policy:** `https://hail.so/legal/privacy`
- **Terms of service:** `https://hail.so/legal/terms`
- **Icon:** 512 × 512 PNG: `https://hail.so/assets/brand/icons/icon-512.png`
- **Access requirements:** A Hail account. Calls and texts need a Hail phone number and credit. Email needs a verified sending domain or a hail.so address.
- **Example prompts:**
  - "Find call-capable Hail numbers in the UK and Canada. Compare monthly fees."
  - "Text +1-415-555-0123 from my Hail number: I am 10 minutes late."
  - "Send an email to the client: the meeting moved to 3pm. Then tell me if it was delivered."
  - "Call the dentist and move my appointment to Friday. Then show me the transcript."

## Tools and classes

Muse's guidelines (5.4, 5.6) ask for each tool's class until the portal has its own field: **Read**, **Write**, or **Sensitive write**. "A tool that combines reads and writes must be classified as a write." The classes below follow the MCP tool hints in `mcp/hailhq/mcp/tools.py`. Sensitive writes are the tools that contact other people, spend money, or cannot be undone. Muse asks the user to approve them every time.

- **Read** (27): `whoami`, `get_call`, `list_calls`, `get_sms`, `list_sms`, `get_email`, `list_emails`, `get_email_raw`, `get_email_attachment`, `get_email_events`, `get_email_stats`, `get_events`, `list_agents`, `get_agent`, `list_contacts`, `lookup_contact`, `list_email_domains`, `list_numbers`, `get_number`, `list_number_regions`, `quote_numbers`, `get_verification_requirements`, `list_verifications`, `get_verification`, `check_verification`, `get_sms_application`, `preview_sms_application`
- **Write** (9): `create_contact`, `create_agent`, `update_agent`, `route_number`, `route_email_domain`, `create_file_upload`, `upload_email_attachment`, `check_verification_documents`, `confirm_verification`
- **Sensitive write** (10): `place_call`, `send_sms`, `send_email` (contact another person), `acquire_number` (charges credit), `release_number`, `delete_agent` (cannot be undone), `submit_verification`, `resubmit_verification`, `amend_sms_application`, `cancel_verification` (legal filings with carriers)

## Notes

- Guidelines 3.1: "offer users the option to limit Muse's permissions to Read Only, where possible." Hail's OAuth has no read-only scope today. Say so in the overview, and point to the per-tool approvals Muse already shows.
- Guidelines 5.7: "Check the portal for your connector's review status. Due to submission volume, we cannot respond to individual messages or emails about status."
- No fee and no country limit are stated (checked 2026-11-18).

- Auth: Muse supports Dynamic Client Registration and client ID metadata documents (CIMD). Hail's OAuth server supports Dynamic Client Registration.
- A dedicated reviewer account exists (created 2026-11-18), with email and password sign-in and prepaid credit. It has no phone number: reviewers can buy one with the credit to test calls and texts. Remove the account after the review ends.
- Never post the reviewer password in a screenshot or a file. Enter it only in the Muse portal.
- Review time: not stated. Manufact reports a Muse email on 2026-10-23 about "more than 4,500 submissions and an onboarding backlog".
- Status: the developer portal at <https://platform.muse.ai/> and the **Manage your submission** link in the confirmation.
