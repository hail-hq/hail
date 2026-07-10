# SMS Inbound Compliance Hardening — Design

Date: 2026-07-10
Status: Approved (brainstorming) — pending implementation plan
Branch context: builds on `feat/sms-inbound-compliance` (inbound webhook, opt-out
ingest, suppression API, abuse monitor already shipped).

## Problem

The inbound-SMS compliance work shipped the receive → suppress → fan-out loop, but
a review + investigation surfaced four gaps that this design closes:

1. **Number FK semantics.** On inbound `Sms` rows, `from_number_id` points at the
   org's *receiving* number (the Twilio `To`), while `from_e164` is the external
   sender. The outbound invariant `from_number_id.e164 == from_e164` is violated.
   `from_number_id` is currently **write-only for SMS** (nothing reads/joins/filters
   on it), so the defect is latent — it bites the first per-number analytics query.
2. **HELP unimplemented.** The spec and plan promised "STOP/HELP/START", but the
   code handles only STOP/START. There is no HELP branch and no auto-reply.
3. **Webhook docs gap.** `sms.received` rides the same signed, retried delivery
   worker as email (full delivery parity), but it is undocumented in
   `docs/setup/webhooks.md`.
4. **Legal disclosures absent.** No legal page names the opt-out keywords or carries
   the CTIA-standard consumer SMS disclosures (reply STOP / HELP, message frequency,
   "message & data rates may apply").

## Key external constraint (drives Workstream 2)

Twilio has **no API** to manage opt-out handling. Advanced Opt-Out is Console-only;
disabling the account's default STOP filtering requires a **support ticket** and
applies **account-wide**. Therefore, out of the box **Twilio itself auto-replies to
STOP/HELP/START and carrier-blocks opted-out numbers**, while still forwarding the
inbound message (with an `OptOutType` param) to our webhook so our suppression record
works. Consequence: if Hail *also* sends its own replies by default, recipients get
**double replies**. So Hail-sent replies must be **opt-in**, not the default.

Sources:
- https://help.twilio.com/articles/223134027-Twilio-support-for-opt-out-keywords-SMS-STOP-filtering-
- https://help.twilio.com/articles/360034798533-Getting-Started-with-Advanced-Opt-Out-for-Messaging-Services

## Scope

Four independently-shippable workstreams across two repos:

| # | Workstream | Repo |
|---|---|---|
| 1 | Split inbound/outbound number FKs (`to_number_id`) | `hail` |
| 2 | HELP/STOP/START replies (opt-in) + keyword completeness | `hail` |
| 3 | Webhook + Twilio setup docs | `hail` |
| 4 | Legal `sms.md` consumer disclosures (draft, review-flagged) | `hail-website` |

Out of scope (log as carry-forwards, not built here):
- Outbound SMS **delivery-status callbacks** (`sms.delivered/sent/failed`) — email
  has 7 lifecycle events; SMS has only `sms.received`. Separate feature.
- Un-suspend tooling for `channel_suspensions` (already a carry-forward in
  `docs/operations.md`).

---

## Workstream 1 — Number FKs (option A)

### Data model
- **Migration `0029`** (Revises `0028`):
  - Add `to_number_id UUID NULL REFERENCES phone_numbers(id)`.
  - `ALTER COLUMN from_number_id ... DROP NOT NULL` (make nullable).
  - No new index — nothing reads either FK yet; add one when a reader lands (YAGNI).
  - No data backfill: existing outbound rows keep `from_number_id`, leave
    `to_number_id` NULL (external destination). No inbound rows predate this in prod.
  - `downgrade()` reverses: drop `to_number_id`, restore `from_number_id NOT NULL`
    (safe only while no NULL rows exist; note this in the migration).
- **`core/hailhq/core/models.py` `Sms`:**
  - `from_number_id: Mapped[uuid.UUID | None]` (nullable).
  - Add `to_number_id: Mapped[uuid.UUID | None]` FK → `phone_numbers.id`.

### Write sites
- **Outbound** (`api/hailhq/api/routes/sms.py` `create_sms`): unchanged —
  `from_number_id = sender.id`, `to_number_id` stays `None` (destination is external).
- **Inbound** (`core/hailhq/core/sms_ingest.py`): set `from_number_id = None`
  (external sender has no `PhoneNumber` row), `to_number_id = number.id` (the org's
  receiving number). Delete the temporary wart comment.

### Interface impact
- **None.** `SmsResponse`/`SmsCreate` expose `from_e164`/`to_e164` as strings, not the
  FKs. No SDK/CLI/openapi change. No `relationship()`/`joinedload` to untangle.

### Tests
- Update the single existing `from_number_id=` write in `core/tests/test_compliance_gate.py`.
- Inbound ingest test: assert `to_number_id == receiving number.id` and
  `from_number_id is None`.
- Outbound test: assert `to_number_id is None`, `from_number_id == sender`.

---

## Workstream 2 — HELP/STOP/START replies (opt-in)

### Keyword handling (`core/hailhq/core/sms_ingest.py`)
- Add `_HELP_KEYWORDS = frozenset({"HELP", "INFO"})`.
- `_opt_out_action(body, opt_out_type)` returns `"STOP" | "START" | "HELP" | None`
  (add the HELP branch; keep `OptOutType` as corroboration).
- STOP → `add_suppression` (existing) **and** send STOP-confirmation reply.
- START → `remove_suppression` (existing) **and** send START-confirmation reply.
- HELP → send HELP reply only (no suppression change).
- Suppression record is written **unconditionally** (independent of the reply flag);
  only the outbound reply is gated.

### Reply mechanism
- New helper `send_compliance_reply(provider, *, from_e164, to_e164, body)`:
  - `from_e164` = the org's receiving number (inbound `to_e164`);
    `to_e164` = the sender (inbound `from_e164`).
  - Calls `SmsProvider.send_sms(...)` directly — **bypasses `check_sms_allowed`**
    (carrier-mandated compliance replies; the STOP confirmation is the single allowed
    post-opt-out message).
  - `provider` is **injected into `ingest_inbound_sms`** from the route's existing
    `get_sms_provider` dependency, so `core` stays provider-neutral.
- Fires **only on the fresh-insert path**, so Twilio retries (idempotent on
  `provider_message_sid`) never double-reply.
- A reply-send failure is **logged and swallowed** — the webhook still returns 200 and
  the suppression/record persists.

### Feature flag + templates (`core/hailhq/core/config.py`, `.env.example`)
- `hail_sms_compliance_replies_enabled: bool = False` (env
  `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED`). Default **false**: Twilio's own opt-out
  handling covers the default deployment; enabling Hail's replies on top of it would
  double-text. Enable only when Twilio's default filtering is disabled (Support
  ticket) or on a non-Twilio provider.
- Reply copy: default templates as settings fields with sensible defaults; carry brand
  "Hail", contact `hi@hail.so`, and "Msg&data rates may apply". Keep them in settings
  so self-hosters can override without a code change. (Note: these are user-facing copy
  strings, not model identifiers, so the "model names live in .env" rule does not
  apply; defaults may live in `config.py`.)
  - STOP: `You are unsubscribed from Hail messages and will receive no more. Reply START to resubscribe. Help: hi@hail.so`
  - HELP: `Hail: for help contact hi@hail.so. Msg&data rates may apply. Reply STOP to unsubscribe.`
  - START: `You are resubscribed to Hail messages. Reply STOP to unsubscribe, HELP for help.`

### Sub-decisions (recorded)
1. **Persist auto-replies as outbound `Sms` rows** — yes, a minimal `direction=outbound`
   row per reply, for an audit trail. Reuses the number FKs from Workstream 1
   (`from_number_id` = org number, `to_number_id` = None).
2. **Bill / usage-event for replies** — no. Platform-mandated compliance traffic, not
   org application traffic; do not write a `UsageEvent` and do not require funds.
3. **HELP synonyms** — `HELP, INFO` only.

### Tests
- HELP body → HELP reply dispatched, no suppression change (mock provider).
- STOP → suppression written + confirmation reply dispatched.
- START → suppression removed + reply dispatched.
- Flag off → suppression still written, **no** reply dispatched.
- Idempotent retry → single reply.
- Reply-send raises → webhook still 200, record intact.

---

## Workstream 3 — Docs (`hail`)

- **`docs/setup/webhooks.md`:** add `sms.received` to the event catalog with an example
  payload (`{id, from, to, body}`), mirroring the `email.received` entry. Note the
  generic signed envelope + `X-Hail-Signature` apply unchanged.
- **`docs/setup/twilio.md`:** document
  - the inbound webhook URL to configure on the Twilio number;
  - the recognized keyword lists (STOP set, START set, HELP set);
  - that **Twilio handles opt-out replies by default**, and to have Hail own them you
    must disable Twilio's default filtering (account-wide Support ticket) and set
    `HAIL_SMS_COMPLIANCE_REPLIES_ENABLED=true`.
- **`README.md`:** tick the inbound-SMS milestone if appropriate (per the
  milestones-list convention, checkbox only).

---

## Workstream 4 — Legal `sms.md` (`hail-website`)

- New `content/legal/sms.md`, consumer-facing, containing:
  - recognized opt-out/opt-in/help keywords;
  - "Reply STOP to opt out at any time; reply HELP for help";
  - message frequency ("recurring; frequency varies by the service you interact with");
  - "Message and data rates may apply";
  - contact `hi@hail.so`;
  - how suppression works (platform-level do-not-contact list).
- Linked from the consent surface and referenced from `aup.md` §5 (opt-out).
- **Marked "DRAFT — requires legal sign-off."** I draft reviewable placeholder copy;
  final wording is a human/lawyer decision.
- Ships as a **separate PR in `hail-website`** with its own review — deliberately not
  bundled with the `hail` code migration.

---

## Build order

1. Workstream 1 (schema) and Workstream 3 (docs) — independent, land first.
2. Workstream 2 (replies + flag) — depends on provider injection into `ingest_inbound_sms`
   and on Workstream 1's number FKs (for the persisted reply rows).
3. Workstream 4 (legal) — separate repo/PR, any time.

## Testing strategy

- Unit tests per workstream as listed above (core + api).
- Full `core` + `api` suites green before each PR.
- Migration `0029` applies and reverses cleanly against the testcontainer Postgres.
- `openapi.yaml` unchanged (no route/schema surface change); CLI codegen not required.

## Risks / notes

- The whole Hail-owns-replies path (Workstream 2, flag on) rests on Twilio's default
  filtering being disabled — an account-wide manual step. The flag defaults off so the
  default deployment is correct without it.
- Making `from_number_id` nullable is safe today (no code dereferences it assuming
  non-null), but any *future* reader must treat it as optional.
- Legal copy must not ship on the model's say-so; the spec flags it for human review.
