# US trial texting: design

## Why

A new workspace cannot text US numbers until its own US registration
(10DLC campaign or toll-free verification) is approved. That takes days.
Trial texting lets a new user send a text to their own phone within
minutes of sign-up, from the API, the MCP server, the CLI or an agent.

Carrier rules forbid sending customer texts to anyone under Hail's own
registration:

- AT&T Code of Conduct: shared 10DLC is prohibited, and one number must not
  let "multiple parties control of content and/or receiving phone numbers".
- Twilio error 30926: a platform must register each customer as its own
  brand. Twilio error 30478: a messaging toll-free number maps 1:1 to one
  business.

The allowed pattern, which SignalWire also runs as its "Platform Free
Trial": Hail is the sender, and trial texts reach only the account owner's
own verified phone, with a fixed prefix and a daily cap.

## Goals

- A workspace with no approved US registration can text one verified phone
  (its owner's) through the normal outbound paths: `POST /v1/sms`, MCP
  `send_sms`, the CLI, and agent tool sends.
- The workspace does not need to own a number.
- Hail sends every trial text from one Hail-owned, verified toll-free
  number.

## Non-goals

- Replies. Trial texts are outbound only. The workspace does not own the
  trial number, so Hail cannot route a reply back to it. Every trial text
  says replies are not read.
- Texting any phone other than the verified one.
- Texts to non-US numbers (they already work without US registration).
- The browser demo on hail.so (`api/hailhq/api/browser_messaging.py`).
- A console "send test text" button.
- Free texts. Trial texts are billed like any outbound text (builder call:
  owner may change).

## Anti-goals (failure even if it works)

- Any path where a trial text reaches a phone that is not the verified
  phone of the sending workspace.
- A trial text without the prefix or the no-reply notice.
- One abusive workspace putting the trial number's verification at risk.

## Decisions

| Topic | Decision | By |
|---|---|---|
| Trial number | Hail's existing toll-free number in Hail's own workspace; one verification covers account notices and trial texts | owner |
| Direction | Outbound only; each text carries a no-reply notice | owner |
| Send paths | Normal API, MCP, CLI and agent tool sends | owner |
| Daily cap | 50 outbound trial texts per workspace per rolling 24 hours | owner |
| Prefix | Every trial text starts with `[Hail trial] ` | owner |
| Who can be texted | Only the workspace's verified trial phone | owner |
| Phone proof | Hail texts a 6-digit code from the trial number; the user types it back in the console | builder call |
| Uniqueness | One verified trial phone per workspace; one workspace per phone | builder call |
| Trial end | When the workspace has any approved US SMS registration | builder call |
| Number needed | None. The trial path runs when `from` is omitted or equals the trial number | builder call |
| Explicit own `from` | A workspace's own unregistered number in `from` keeps today's 422; the message names the trial | builder call |
| Billing | Normal outbound text price from credits | builder call |

## How it works

1. **Find the trial number.** A setting holds the trial number's E.164
   (empty = feature off, the self-host default). The number stays a normal
   row in Hail's own workspace, so its toll-free verification is filed and
   tracked through the normal flow.
2. **Verify the phone.** The console asks for a mobile number and a
   consent tick. Hail texts a 6-digit code from the trial number. The code
   expires in 10 minutes, allows 5 tries, and is stored hashed. On success
   the phone is the workspace's verified trial phone. A phone already
   verified by another workspace is refused.
3. **Send.** One shared helper decides the trial path. It runs at every
   outbound call site of `us_registration_block`:
   - `api/hailhq/api/routes/sms.py` (public send)
   - `api/hailhq/api/routes/internal/agent.py`: agent tool send, send
     during a voice call, handover notice
   The helper allows a trial send when all are true:
   - the workspace has no approved US SMS registration
   - `to` equals the workspace's verified trial phone
   - the workspace is under the cap
   It then sends from the trial number. The body becomes
   `[Hail trial] <body>` plus a short no-reply line. Both count toward
   segment count and body length limits. Otherwise the call site keeps
   today's 422.
   The trial path must run before the "no dedicated SMS-capable number"
   422 in `sms.py`, so a workspace with no number can use it.
4. **Inbound.** Nothing new. Texts to the trial number arrive in Hail's
   own workspace as today. Twilio handles STOP, HELP and START on
   toll-free numbers (builder: confirm in Twilio docs). After STOP, Twilio
   refuses further texts to that phone. Hail shows that refusal on the
   workspace's text record.
5. **End.** Once the workspace has an approved US registration, the trial
   path stops. Sends use the workspace's own registered number.

## Toll-free verification

One filing for the number covers both uses. The opt-in evidence must show
both: the sign-up SMS box (account notices) and the console phone step
(trial texts). A use case change on an approved filing means deleting it
and filing again, so file the combined text once the console step is live.

## Constraints

- Follow the schema rules in `CLAUDE.md`: reuse an existing table first.
  `email_forward_targets` is the closest kind of record (a destination
  proven by the user). The builder decides between extending it with a
  channel column and a new table, and writes the reason in the migration.
- The new setting follows the `.env` rules in `CLAUDE.md`.
- No prices in this repo. Billing goes through the existing rater.
- Regenerate `openapi/openapi.yaml` after route changes.
- The console part ships in hail-website after the hail deploy.

## Left to the builder

- Table shape and migration.
- The exact 422 wording, the no-reply line and the console copy
  (ASD-STE100).
- How the console shows the cap and remaining texts.
- Tests: every item in "Anti-goals" gets a test that fails without the
  guard, at every call site in "Send".
