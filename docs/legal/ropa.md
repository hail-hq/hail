# Records of Processing Activities (RoPA)

> This record is maintained on an ongoing basis; it is reviewed and
> updated whenever a sub-processor, channel, or data-handling practice
> changes (see the maintenance checklist in `hail-website/content/legal/
facts.md`, the source of truth this record is kept consistent with).
> A small number of cells are marked `[NEEDS LAWYER INPUT: ...]` — open
> legal questions pending outside counsel, not gaps in the record itself.

This is Hail's record per GDPR Art. 30(1) (records of a **controller**) and
Art. 30(2) (records of a **processor**). Hail HQ ("Hail") holds both
roles depending on the activity — see `facts.md` § Definitions:

- **Processor** for Developer's Recipient/customer data processed to deliver
  the service (voice calls, email).
- **Controller** for Hail's own account/billing and website data.

## 0. Controller / processor identity block

| Field                                   | Value                                                                                                                                                            |
| --------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Controller/processor name               | Hail HQ                                                                                                                                                          |
| Legal form                              | Swedish aktiebolag (AB)                                                                                                                                          |
| Registered address                      | Not published — identified by name + hi@hail.so contact only (founder decision, 2026-07-07)                                                                      |
| Representative (if applicable, Art. 27) | **N/A** — Art. 27 applies to controllers/processors not established in the Union; Hail HQ is established in the EU (Sweden), so this requirement does not apply. |
| DPO / privacy contact                   | Not appointed (open item, tracked in the master TODO: "Decide DPO: appoint or document why not")                                                                 |
| Joint controller arrangements           | None known. `[NEEDS LAWYER INPUT: confirm]`                                                                                                                      |

## 1. Per-activity record template

Each processing activity below follows this schema. Where a fact is already
established (per `facts.md`), it is pre-filled. Where it is not yet decided,
the cell is left as an explicit prompt for the founder/lawyer to complete —
**do not fabricate a value**.

| Field                       | What goes here                                                                                                                                                                                                    |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Activity name               | Short label                                                                                                                                                                                                       |
| Role                        | Controller or Processor (per definitions above)                                                                                                                                                                   |
| Purpose(s) of processing    | Why the data is processed                                                                                                                                                                                         |
| Legal basis                 | Contract / legitimate interest / consent / legal obligation — cite which, and for whom (Hail's basis as controller, or the Developer's basis as controller for Recipient data, which Hail relies on as processor) |
| Categories of data subjects | e.g. Developers, Recipients, Hail's own staff                                                                                                                                                                     |
| Categories of personal data | What fields/content                                                                                                                                                                                               |
| Special category data?      | Y/N — flag if call/email content could incidentally reveal Art. 9 special category data                                                                                                                           |
| Recipients / sub-processors | Which of the 14 confirmed sub-processors (see § 5) touch this activity's data, and why                                                                                                                            |
| Third-country transfers     | Which sub-processors are outside the EEA, and the transfer safeguard relied on (SCCs, adequacy, etc.)                                                                                                             |
| Retention period            | Per `facts.md` § Data retention, or activity-specific                                                                                                                                                             |
| Security measures (Art. 32) | Technical/organizational measures in place                                                                                                                                                                        |

---

## 2. Activity: Outbound voice calls

| Field                       | Content                                                                                                                                                                                                                                                                                                                                                               |
| --------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Role                        | Processor (Recipient data)                                                                                                                                                                                                                                                                                                                                            |
| Purpose(s)                  | Developer triggers outbound AI voice calls programmatically (MCP / REST API / CLI) to Recipients                                                                                                                                                                                                                                                                      |
| Legal basis                 | Hail's processing basis: Developer's instructions under the DPA. Developer's own lawful basis for contacting the Recipient (consent/legitimate interest under TCPA/ePrivacy/GDPR) is the Developer's responsibility per `facts.md` § KYC/consent posture — `[NEEDS LAWYER INPUT: confirm framing of Hail's Art. 28 basis vs. Developer's Art. 6 basis in the record]` |
| Categories of data subjects | Recipients (call recipients); Developers (account/API identity triggering the call)                                                                                                                                                                                                                                                                                   |
| Categories of personal data | Recipient phone number; call metadata (duration, timestamps, status); call audio (in-transit only — **not recorded or stored**, per `facts.md`); text transcript of the call (stored)                                                                                                                                                                                 |
| Special category data?      | Possible incidental exposure if a Recipient volunteers health/political/other Art. 9 data during a call — `[NEEDS LAWYER INPUT: whether this requires a distinct Art. 9 basis or DPIA-level mitigation only — see dpia-summary.md]`                                                                                                                                   |
| Recipients / sub-processors | Twilio (telephony numbers, SIP carrier); LiveKit (SFU/room, SIP dial-out — audio in-transit only); Deepgram (speech-to-text); Cartesia and ElevenLabs (text-to-speech); OpenAI, Google, Anthropic (LLM fallback chain — processes transcript/conversation content)                                                                                                    |
| Third-country transfers     | Hail operates as a global service; per-vendor regions are not tracked/published (founder decision, 2026-07-07). SCCs (incorporated by reference in the DPA) apply as a blanket safeguard regardless of specific sub-processor location.                                                                                                                               |
| Retention period            | Duration of Developer's account + 12 months after account closure (call transcripts). Audio: not stored at all (v1 stub, no recording path).                                                                                                                                                                                                                          |
| Security measures           | Encryption in transit/at rest, access controls, least-privilege, and audit logging — per DPA Annex II (`hail-website/content/legal/dpa.md`).                                                                                                                                                                                                                          |
| Notes                       | —                                                                                                                                                                                                                                                                                                                                                                     |

## 2a. Activity: Outbound SMS/text messaging

> ⚠ **Sequencing note (2026-07-07):** the public legal docs (ToU/AUP/Privacy/DPA)
> already describe SMS as a live channel, per explicit founder direction, ahead
> of the code shipping and ahead of the DPIA review below being completed. The
> approved engineering spec
> (`docs/superpowers/specs/2026-07-06-sms-support-design.md`) itself flags this
> ordering risk explicitly: "DPIA review is an explicit pre-launch gate...
> don't treat the legal-doc-flip... as sufficient." **This DPIA review is a
> hard gate before the SMS channel actually goes live in production**, not a
> formality to complete after the fact.

| Field                       | Content                                                                                                                                                                                                                |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Role                        | Processor (Recipient data)                                                                                                                                                                                             |
| Purpose(s)                  | Developer triggers outbound SMS/text messages programmatically (MCP / REST API / CLI) to Recipients                                                                                                                    |
| Legal basis                 | Same framing as voice calls above — `[NEEDS LAWYER INPUT: confirm framing of Hail's Art. 28 basis vs. Developer's Art. 6 basis in the record]`                                                                         |
| Categories of data subjects | Recipients (message recipients); Developers                                                                                                                                                                            |
| Categories of personal data | Recipient phone number; message content; message metadata (timestamps, delivery/status events)                                                                                                                         |
| Special category data?      | Possible incidental exposure if a Recipient's reply or message content includes health/political/other Art. 9 data — `[NEEDS LAWYER INPUT]`                                                                            |
| Recipients / sub-processors | Twilio (Messaging Service, carrier, A2P 10DLC registration)                                                                                                                                                            |
| Third-country transfers     | Global service, no per-vendor region tracking; SCCs apply as a blanket safeguard (see § 2 above).                                                                                                                      |
| Retention period            | Duration of Developer's account + 12 months after account closure (same policy as voice/email)                                                                                                                         |
| Security measures           | Same as voice calls — encryption in transit/at rest, access controls, least-privilege, audit logging (DPA Annex II).                                                                                                   |
| Notes                       | Reuses the generic `Suppression`/`enforce_consent`/`compliance_gate` system built for voice/email (per the SMS spec) rather than a channel-specific equivalent. TCPA applies to SMS the same as voice calls in the US. |

## 3. Activity: Outbound email

| Field                       | Content                                                                                                                                                                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Role                        | Processor (Recipient data)                                                                                                                                                                                                                 |
| Purpose(s)                  | Developer triggers outbound product/transactional email to Recipients programmatically                                                                                                                                                     |
| Legal basis                 | Same framing as voice calls above — `[NEEDS LAWYER INPUT]`                                                                                                                                                                                 |
| Categories of data subjects | Recipients (email recipients); Developers                                                                                                                                                                                                  |
| Categories of personal data | Recipient email address; email content (raw MIME)                                                                                                                                                                                          |
| Special category data?      | Possible incidental exposure in email body — `[NEEDS LAWYER INPUT]`                                                                                                                                                                        |
| Recipients / sub-processors | AWS (SES for sending, S3 for MIME storage/audit)                                                                                                                                                                                           |
| Third-country transfers     | Global service, no per-vendor region tracking; SCCs apply as a blanket safeguard (see § 2 above).                                                                                                                                          |
| Retention period            | Duration of Developer's account + 12 months after account closure (stored email content)                                                                                                                                                   |
| Security measures           | Same as voice calls — encryption in transit/at rest, access controls, least-privilege, audit logging (DPA Annex II).                                                                                                                       |
| Notes                       | Resend is used **only** for Hail's own auth/transactional email (signup confirmation, password reset) — it is a separate activity (see § 4a below), not part of Developer-triggered product email. Do not conflate the two in this record. |

### 3a. Activity: Hail's own auth/transactional email (controller)

| Field                       | Content                                                                                                                                                                                                                         |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Role                        | Controller (this is Hail's own operational email to its Developers, not Developer-triggered)                                                                                                                                    |
| Purpose(s)                  | Signup confirmation, password reset, other account-lifecycle email to Developers                                                                                                                                                |
| Legal basis                 | `[NEEDS LAWYER INPUT: likely contract/legitimate interest]`                                                                                                                                                                     |
| Categories of data subjects | Developers (Hail's direct customers/account holders)                                                                                                                                                                            |
| Categories of personal data | Developer email address, account-lifecycle event content                                                                                                                                                                        |
| Recipients / sub-processors | Resend                                                                                                                                                                                                                          |
| Third-country transfers     | Global service, no per-vendor region tracking; safeguards per Privacy Policy § 6 (`hail-website/content/legal/privacy.md`).                                                                                                     |
| Retention period            | Not retained by Hail beyond the send itself — confirmed by founder (2026-07-08). No persistent copy of auth-email content is stored in Hail's own database; Resend's own transactional logs are outside Hail's retention scope. |
| Security measures           | Same as voice calls — encryption in transit/at rest, access controls, least-privilege, audit logging (DPA Annex II).                                                                                                            |

## 4. Activity: Account / billing

| Field                       | Content                                                                                                                                                                                                                                                                                                                                           |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Role                        | Controller                                                                                                                                                                                                                                                                                                                                        |
| Purpose(s)                  | Account creation/management, authentication, subscription and billing administration for Developers                                                                                                                                                                                                                                               |
| Legal basis                 | Contract (ToU) for account administration; contract/legal obligation for billing records                                                                                                                                                                                                                                                          |
| Categories of data subjects | Developers (and their end-users/staff who hold logins)                                                                                                                                                                                                                                                                                            |
| Categories of personal data | Account/config data, application usage data, billing contact info, payment data (tokenized)                                                                                                                                                                                                                                                       |
| Recipients / sub-processors | Supabase (database/backend infra); Stripe (billing/payments)                                                                                                                                                                                                                                                                                      |
| Third-country transfers     | Global service, no per-vendor region tracking; SCCs apply as a blanket safeguard (see § 2 above).                                                                                                                                                                                                                                                 |
| Retention period            | Billing/accounting records: 7 years, per Swedish bookkeeping law (Bokföringslagen) minimum retention for accounting records — a statutory floor, not a preference. Other account data (profile, config): duration of account + 12 months after closure (founder decision, 2026-07-08 — same policy as transcripts/email, one number to remember). |
| Security measures           | Same as voice calls — encryption in transit/at rest, access controls, least-privilege, audit logging (DPA Annex II).                                                                                                                                                                                                                              |

## 4a. Activity: Website / product analytics (controller)

| Field                       | Content                                                                                                                                                                                                                                                                                                                                                                             |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Role                        | Controller                                                                                                                                                                                                                                                                                                                                                                          |
| Purpose(s)                  | Product analytics on the website and console; understanding usage patterns                                                                                                                                                                                                                                                                                                          |
| Legal basis                 | **Legitimate interest** — updated 2026-07-07: analytics now run on-by-default (not gated behind prior consent), so consent is not the operative basis being relied on for the default-on population; visitors can opt out via the cookie banner. `[NEEDS LAWYER INPUT: confirm legitimate interest is defensible given the on-by-default posture, particularly for EU/UK visitors]` |
| Categories of data subjects | Website visitors, Developers using the console                                                                                                                                                                                                                                                                                                                                      |
| Categories of personal data | Usage/behavioral data, IP address                                                                                                                                                                                                                                                                                                                                                   |
| Recipients / sub-processors | PostHog (analytics); Vercel (hosting — sees web traffic/request data)                                                                                                                                                                                                                                                                                                               |
| Third-country transfers     | Global service, no per-vendor region tracking; safeguards per Privacy Policy § 6 (`hail-website/content/legal/privacy.md`). PostHog is configured to process EU data in the EU per the Cookie Policy.                                                                                                                                                                               |
| Retention period            | **12 months** (founder decision, 2026-07-08). Not yet enforced in PostHog's actual configuration — configuring a 12-month retention policy in PostHog is a follow-up implementation task, tracked in the master TODO.                                                                                                                                                               |
| Security measures           | Same as voice calls — encryption in transit/at rest, access controls, least-privilege, audit logging (DPA Annex II).                                                                                                                                                                                                                                                                |

## 5. Sub-processor reference (conceptual — do not fork this list)

The 14 confirmed sub-processors and their roles are maintained authoritatively
in `hail-website/content/legal/facts.md` and rendered publicly from
`hail-website/content/legal/subprocessors.json`. Reference them by name in the
per-activity rows above; **do not copy/maintain a second independent list
here** — link back and keep this doc pointing at the single source of truth
to avoid drift. As of this writing the confirmed set is: Supabase, Stripe,
Twilio, LiveKit, AWS, Deepgram, Cartesia, ElevenLabs, OpenAI, Google,
Anthropic, PostHog, Vercel, Resend.

## 6. Audit logs (tracked separately)

Audit-log retention is a distinct processing/retention question from the
transcript/email retention above. Founder decision (2026-07-07): **3 years**
from the date the log entry is created, for security/fraud-investigation
purposes.

## 7. Self-hosted deployments — out of scope

Per `facts.md` § Scope, self-hosted (AGPLv3) deployments are run by an
independent controller/operator. Hail HQ has no
processor/controller relationship, no visibility into, and no liability for
self-hosted instances. **This RoPA does not and should not cover self-hosted
deployments.**

## 8. Review cadence

Same triggers as the DPIA (`docs/legal/dpia-summary.md`): before GA; before
the SMS channel launches; on any new sub-processor; on any material
architecture change; at minimum annually; and re-assessed as usage scales.
