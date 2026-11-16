# US Trial Texting (hail API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A workspace with no approved US registration can send up to 25 texts, ever, to its own verified US mobile phone, from Hail's toll-free number, through `POST /v1/sms`, MCP, the CLI and agent sends.

**Architecture:** One core module, `hailhq.core.sms_trial`, owns the setting lookup, the phone proof (6-digit code), the body wrapper and the race-safe cap. Every outbound call site of `us_registration_block` (except replies to inbound texts and the browser messaging capability check) asks `claim_trial_send` when its normal sender is missing or blocked. The proven phone lives in a new `sms_trial_phones` table; trial texts are normal `sms` rows in the sending workspace, marked by a server-only metadata key.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic (hand-written), Pydantic v2, pytest (async), Go CLI codegen, FastMCP.

**Spec:** `docs/superpowers/specs/2026-11-15-us-trial-texting-design.md`. Owner decisions in this plan override the spec where they differ.

## Global Constraints

- Trial texts are outbound only. No reply routing.
- Body sent = `"[Hail trial] " + body + "\nHail does not read replies."`. Prefix and footer count toward the body limit and segment count.
- Cap: 25 trial texts per workspace, ever. No daily limit, no end date. An approved US registration does not end the trial.
- A `from`-less send to the verified phone prefers the workspace's own registered number (`registered_sms_number`); that text is not a trial text.
- The trial number is a normal `phone_numbers` row in Hail's own workspace, found by the setting `HAIL_SMS_TRIAL_NUMBER` (E.164). Empty = feature off (self-host default).
- Sending needs two things (`trial_sender`, Task 1): the trial number is on an approved registration of its owner org, AND `HAIL_SMS_TRIAL_SENDING=true`. The owner files the toll-free verification for account notices first; that approval alone must not start trial sending, because trial texts are a second use case filed later. The owner turns `HAIL_SMS_TRIAL_SENDING` on only after the combined filing (notices + trial) is approved. Until then no code or trial text is sent; `GET /v1/sms/trial` says `configured: true, available: false`, so the console shows the form for the filing's opt-in evidence.
- Only the workspace's verified trial phone receives trial texts. One row per workspace; one workspace per verified phone (DB partial unique index).
- Phone proof: 6-digit code, 10 minutes, 5 tries, stored hashed, at most 5 code texts per workspace per rolling 24 hours. Code texts are not `sms` rows, are not billed, and do not count toward the 25.
- Trial path runs when `from` is omitted or equals the trial number, and before the "no dedicated SMS-capable phone number" 422 in `api/hailhq/api/routes/sms.py`. An explicit own unregistered `from` keeps today's 422, and its message names the trial.
- Hail's own workspace (the trial number's owner) never takes the trial path.
- Billing: normal outbound text via `deliver_sms(..., billable=True)`. No prices in this repo.
- Not in scope: `agent_reply_sms` (`api/hailhq/api/routes/internal/agent.py`, replies to inbound), `api/hailhq/api/browser_messaging.py`.
- Schema rules from `CLAUDE.md`: no FK to website-owned `organizations`; CHECK constraints; partial unique indexes; migration docstring names the tables checked.
- `.env.example` and `.env` stay line-for-line equal; the parity diff prints nothing.
- Public repo: no real phone numbers, customer names, internal hosts, VM paths, or migration numbers in docs, CHANGELOG or PR body. Fixtures use `+1800555019x` and `+1415555014x`.
- API error text never names a carrier (the console shows it as is).
- Regenerate `openapi/openapi.yaml` after route or schema changes.
- Python: ruff + black; Conventional Commits; no AI attribution trailers.

## Review Focus

1. A phone that is pending in two workspaces: the second confirm must get a 409, not a 500 from the unique index.
2. A user who types a Hail number (any `phone_numbers` row) as "my mobile": refuse it, or code texts land in another workspace's inbox.
3. Twilio reports STOP refusals (21610) after the send, in the status callback: the row must get `error_code`, and failed rows must not use up the cap.
4. A long body at each call site: the footer must survive (refuse on the public API and agent tool sends, cut the inner words on the handover notice).
5. A caller who puts the marker key in `metadata` on normal sends: refuse it with 422, so nobody can plant or fake trial rows.

Each line has a test in the task that owns the code (Tasks 3, 3, 8, 6/7, 6).

## File Structure

- Create `core/hailhq/core/sms_trial.py`: constants, setting lookup, body wrapper, phone proof, cap claim.
- Create `api/hailhq/api/routes/sms_trial.py`: `GET /v1/sms/trial`, `POST /v1/sms/trial/phone`, `POST /v1/sms/trial/phone/confirm`.
- Create `api/migrations/versions/<next>_sms_trial_phones.py` (`<next>` = the number after the newest file in `api/migrations/versions` on `origin/main` at build time; `revision`/`down_revision` follow).
- Modify `core/hailhq/core/models.py` (new `SmsTrialPhone`, partial index on `Sms`), `core/hailhq/core/config.py`, `core/hailhq/core/schemas.py`, `.env.example`, `api/hailhq/api/main.py`, `api/hailhq/api/routes/sms.py`, `api/hailhq/api/routes/internal/agent.py`, `api/hailhq/api/routes/sms_webhooks/twilio.py`, `mcp/hailhq/mcp/tools.py`, `cli/internal/cmd/sms.go`, `cli/internal/client/client.gen.go`, `openapi/openapi.yaml`, `docs/public/self-host/twilio.md`, `CHANGELOG.md`.
- Tests: `core/tests/test_sms_trial.py`, `api/tests/test_sms_trial_phones_migration.py`, `api/tests/test_sms_trial_api.py`, `api/tests/test_sms_trial_send.py`, `api/tests/test_internal_agent_trial.py`, `api/tests/test_sms_api.py` (status callback), `api/tests/conftest.py` (helpers).

### Why a new table, not `email_forward_targets`

`email_forward_targets` is the closest record (a destination the user proved), but extending it breaks it:

- `email_forward_targets_token_hash_idx` is a global unique index on `token_hash`. A 6-digit code has 1,000,000 values, so two workspaces' code hashes collide often enough to raise `IntegrityError` for real users.
- Its uniqueness is (organization, address), many rows per org. The trial needs one row per org and one org per verified phone.
- Every query in `core/hailhq/core/forward_targets.py` and the inbound forward path reads `status='verified'` with no channel filter. One missed filter makes a phone row an email forward address.
- It needs a try counter and a code-send window; forward targets have neither.

Also checked: `contacts` and `agent_handover_contacts` (directory people, no proof state), `suppressions` (opposite meaning), `carrier_verifications` (carrier filings), website `users.sms_opt_in_*` (per user, account notices, no proof). The migration docstring repeats this list.

---

### Task 1: Setting, trial number lookup, body wrapper

**Files:**
- Modify: `core/hailhq/core/config.py` (next to `sender_id_sms_carrier`, ~line 119)
- Modify: `.env.example` (after `SENDER_ID_SMS_CARRIER=twilio`, ~line 169)
- Create: `core/hailhq/core/sms_trial.py`
- Test: `core/tests/test_sms_trial.py`

**Interfaces:**
- Produces (in `hailhq.core.sms_trial`):
  - `SMS_TRIAL_LIMIT: int = 25`
  - `SMS_TRIAL_META_KEY: str = "sms_trial"` (value stored: `True`)
  - `TRIAL_PREFIX: str = "[Hail trial] "`
  - `TRIAL_FOOTER: str = "\nHail does not read replies."`
  - `TRIAL_HINT: str` (exact copy below)
  - `def trial_enabled() -> bool` (setting non-empty)
  - `def trial_body(body: str, *, limit: int, truncate: bool = False) -> str | None`: `TRIAL_PREFIX + body + TRIAL_FOOTER` when it fits in `limit`; with `truncate=True` the inner body is cut so the result is exactly `limit` long at most and the footer stays; otherwise `None`.
  - `async def trial_number(db: AsyncSession) -> PhoneNumber | None`: the active, sms-capable `PhoneNumber` whose `e164 == settings.hail_sms_trial_number`; `None` when the setting is empty or no row matches. ("configured")
  - `async def trial_sender(db: AsyncSession) -> PhoneNumber | None`: `trial_number(db)` when `settings.hail_sms_trial_sending` is true AND it is on an approved registration of its owner org (`on_approved_registration(db, number.organization_id, number)` from `hailhq.core.sms_registration_gate`), else `None`. ("ready"). DB only: no live carrier call, because `GET /v1/sms/trial` runs it on every Numbers page load. This is the one availability check: `GET`, `POST /sms/trial/phone` and `claim_trial_send` all use it.
- Produces (in `hailhq.core.config.Settings`): `hail_sms_trial_number: str = ""`, validated empty or E.164 (`^\+[1-9]\d{6,14}$`); bad value fails startup. `hail_sms_trial_sending: bool = False`.

`TRIAL_HINT` copy (appended to US 422 messages when `trial_enabled()`):
`"To test now, verify your own mobile under Numbers > Trial texting, then send to it without from."`

`.env.example` lines (and the same lines in `.env`, see Task 9):

```
# US trial texting: E.164 of the toll-free number in Hail's own workspace
# that sends up to 25 trial texts to each workspace's verified phone.
# Empty = off.
HAIL_SMS_TRIAL_NUMBER=
HAIL_SMS_TRIAL_SENDING=false
```

- [ ] **Step 1: Write the failing tests**

```python
def test_trial_body_wraps_prefix_and_footer():
    assert trial_body("hi", limit=1600) == "[Hail trial] hi\nHail does not read replies."

def test_trial_body_refuses_when_too_long():
    overhead = len(TRIAL_PREFIX) + len(TRIAL_FOOTER)
    assert trial_body("x" * (1600 - overhead), limit=1600) is not None
    assert trial_body("x" * (1601 - overhead), limit=1600) is None

def test_trial_body_truncate_keeps_footer():
    out = trial_body("x" * 2000, limit=480, truncate=True)
    assert len(out) == 480 and out.startswith(TRIAL_PREFIX) and out.endswith(TRIAL_FOOTER)

async def test_trial_number_off_when_setting_empty(async_session, monkeypatch):
    monkeypatch.setattr(settings, "hail_sms_trial_number", "")
    assert await trial_number(async_session) is None

async def test_trial_number_finds_active_sms_row(async_session, monkeypatch):
    # seed toll-free +18005550199, capabilities ["sms"], active, other org
    assert (await trial_number(async_session)).e164 == "+18005550199"

async def test_trial_number_ignores_inactive_or_voice_only(async_session, monkeypatch): ...

async def test_trial_sender_needs_an_approved_registration(async_session, monkeypatch):
    # number seeded, no approved toll-free CarrierVerification -> trial_number is set, trial_sender is None
    # approved row with number_id + sms_registration_id, HAIL_SMS_TRIAL_SENDING false -> trial_sender is None
    # same, HAIL_SMS_TRIAL_SENDING true -> trial_sender returns the number

def test_setting_rejects_non_e164():
    with pytest.raises(ValidationError):
        Settings(hail_sms_trial_number="18005550199")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v`
Expected: FAIL with `ModuleNotFoundError: hailhq.core.sms_trial`

- [ ] **Step 3: Implement the setting, `.env.example` lines, constants, `trial_body`, `trial_number`, `trial_sender`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/config.py core/hailhq/core/sms_trial.py core/tests/test_sms_trial.py .env.example
git commit -m "feat(sms): trial texting setting and body wrapper"
```

---

### Task 2: `sms_trial_phones` table and the `sms` trial index

**Files:**
- Create: `api/migrations/versions/<next>_sms_trial_phones.py`
- Modify: `core/hailhq/core/models.py` (new class after `SmsSenderIdentity`; index added to `Sms.__table_args__`)
- Test: `api/tests/test_sms_trial_phones_migration.py` (pattern: `api/tests/test_tollfree_verification_migration.py`, `pytestmark = pytest.mark.recreate_schema`)

**Interfaces:**
- Produces `hailhq.core.models.SmsTrialPhone` (`__tablename__ = "sms_trial_phones"`):
  - `id: UUID` PK, `gen_random_uuid()`
  - `organization_id: UUID` not null, no FK (website-owned table), UNIQUE `sms_trial_phones_org_uq`
  - `phone_e164: str` not null
  - `status: str` default `'pending'`, CHECK `sms_trial_phones_status_check`: `status IN ('pending','verified')`
  - `code_hash: str | None`, `code_expires_at: datetime | None`, `code_attempts: int` default 0
  - `code_send_log: list` JSONB not null default `'[]'`: one `{"phone", "at"}` entry per code text, pruned to the last 24 hours on write (rolling per-workspace and per-phone budgets; a phone-keyed `key_lock` guards the per-phone count)
  - `consent_version: str` not null, `consent_at: datetime` not null, `consent_user_id: UUID | None`
  - `verified_at: datetime | None`, `created_at`, `updated_at` (`now()`)
  - Partial unique index `sms_trial_phones_verified_phone_uq` on `(phone_e164)` WHERE `status = 'verified'`
- Produces on `Sms.__table_args__`: `Index("sms_trial_org_idx", "organization_id", postgresql_where=text("metadata ? 'sms_trial'"))`. Tests build the schema with `Base.metadata.create_all`, so the model must carry every index the migration adds.

Migration docstring: the "Why a new table" list above, in short lines.

- [ ] **Step 1: Write the failing migration test**

```python
async def test_sms_trial_phones_round_trip(db):
    # downgrade(); upgrade() via Operations, as in the toll-free migration test
    assert "sms_trial_phones_verified_phone_uq" in indexdefs and "WHERE (status = 'verified'" in that_def
    assert "sms_trial_org_idx" in sms_indexdefs and "metadata ? 'sms_trial'" in that_def

async def test_one_workspace_per_verified_phone(async_session):
    # two orgs, same phone, both verified -> IntegrityError on flush
async def test_two_pending_rows_for_one_phone_are_allowed(async_session): ...
async def test_one_row_per_workspace(async_session):
    # second row same org -> IntegrityError
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd api && uv run pytest tests/test_sms_trial_phones_migration.py -v`
Expected: FAIL (file not found / `SmsTrialPhone` import error)

- [ ] **Step 3: Write the model and the migration (`upgrade` creates table + both indexes; `downgrade` drops them)**

- [ ] **Step 4: Run to verify it passes, then run the full migration chain**

Run: `cd api && uv run pytest tests/test_sms_trial_phones_migration.py tests/test_migrations.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/migrations/versions/*_sms_trial_phones.py core/hailhq/core/models.py api/tests/test_sms_trial_phones_migration.py
git commit -m "feat(sms): sms_trial_phones table"
```

---

### Task 3: Phone proof (start and confirm)

**Files:**
- Modify: `core/hailhq/core/sms_trial.py`
- Test: `core/tests/test_sms_trial.py`

**Interfaces:**
- Consumes: `SmsTrialPhone` (Task 2), `is_us` from `hailhq.core.sms_registration_gate`, `org_lock` from `hailhq.core.db`.
- Produces:
  - `CODE_TTL = timedelta(minutes=10)`, `MAX_CODE_TRIES = 5`, `MAX_CODE_SENDS_PER_DAY = 5`, `CODE_WINDOW = timedelta(hours=24)`
  - `def code_text(code: str) -> str` returns `f"Your Hail trial code is {code}. It expires in 10 minutes. Reply STOP to opt out, HELP for help."`
  - Exceptions, `str(exc)` is the user-facing copy:
    - `PhoneNotUs`: "Trial texts go to US mobile numbers only."
    - `PhoneIsHailNumber`: "Use your own mobile phone, not a Hail number."
    - `PhoneTaken`: "Another workspace uses this phone for trial texts. Use a different phone."
    - `CodeBudgetExceeded`: "You asked for 5 codes today. Try again tomorrow." (carries `retry_after: timedelta`)
    - `NoPendingCode`: "Send a code first."
    - `CodeExpired`: "The code expired. Send a new code."
    - `WrongCode(tries_left: int)`: "The code is not correct. {n} tries left." (0 tries left: "The code is not correct. Send a new code.")
  - `@dataclass(frozen=True) class IssuedCode: row: SmsTrialPhone; code: str | None` (`code is None` when the same phone is already verified: no text is sent)
  - `async def get_trial_phone(db, organization_id: UUID) -> SmsTrialPhone | None`
  - `async def is_verified_trial_phone(db, organization_id: UUID, to: str) -> bool` (the org's row is `verified` and `phone_e164 == to`)
  - `async def start_phone_proof(db, organization_id: UUID, *, phone: str, consent_version: str, user_id: UUID | None, now: datetime | None = None) -> IssuedCode`: takes `org_lock`; checks in order `PhoneNotUs`, `PhoneIsHailNumber` (any `phone_numbers` row with that `e164`), `PhoneTaken` (another org's verified row), `CodeBudgetExceeded`; upserts the org's row (a new phone resets `status='pending'`, `verified_at=None`); writes `code_hash`, `code_expires_at`, `code_attempts=0`, consent fields; appends a `{phone, at}` entry to `code_send_log` (rolling 24 hours). Caller commits after the text is sent.
  - `async def confirm_phone(db, organization_id: UUID, code: str, *, now: datetime | None = None) -> SmsTrialPhone`: takes `org_lock`; `NoPendingCode` / `CodeExpired` / `WrongCode`; the 5th wrong try clears `code_hash`; success sets `status='verified'`, `verified_at`, clears code fields, flushes, and maps the unique-index `IntegrityError` to `PhoneTaken`.
- Code: `f"{secrets.randbelow(10**6):06d}"`. Hash: `sha256(f"{organization_id}:{phone}:{code}")` hex; compare with `hmac.compare_digest`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_start_issues_six_digit_code_and_stores_hash_only(async_session):
    issued = await start_phone_proof(s, org, phone="+14155550142", consent_version="v1", user_id=None)
    assert re.fullmatch(r"\d{6}", issued.code)
    assert issued.row.code_hash != issued.code and issued.code not in issued.row.code_hash
    assert issued.row.code_expires_at - now == timedelta(minutes=10)

async def test_start_refuses_non_us_phone(...):            # "+447700900123" -> PhoneNotUs
async def test_start_refuses_a_hail_number(...):           # phone_numbers row with that e164 -> PhoneIsHailNumber
async def test_start_refuses_phone_verified_elsewhere(...):# other org verified -> PhoneTaken
async def test_sixth_code_in_a_day_is_refused(...):        # 5 starts ok, 6th CodeBudgetExceeded; at now+24h ok
async def test_same_verified_phone_sends_no_code(...):     # issued.code is None, status stays verified
async def test_new_phone_resets_to_pending(...):

async def test_confirm_right_code_verifies(...):
async def test_confirm_after_ten_minutes_is_expired(...):  # now + 10m + 1s -> CodeExpired
async def test_fifth_wrong_try_burns_the_code(...):
    # 4 wrong -> WrongCode(tries_left=1..4 descending); 5th -> tries_left=0; right code then -> NoPendingCode
async def test_confirm_race_second_workspace_gets_phone_taken(...):
    # org A and org B pending on the same phone; A confirms; B confirm -> PhoneTaken (not IntegrityError)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v -k "start or confirm"`
Expected: FAIL with `ImportError: start_phone_proof`

- [ ] **Step 3: Implement the constants, exceptions, `IssuedCode`, `get_trial_phone`, `start_phone_proof`, `confirm_phone`**

- [ ] **Step 4: Run to verify they pass**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/sms_trial.py core/tests/test_sms_trial.py
git commit -m "feat(sms): trial phone proof with a texted code"
```

---

### Task 4: Race-safe trial claim

**Files:**
- Modify: `core/hailhq/core/sms_trial.py`
- Test: `core/tests/test_sms_trial.py`

**Interfaces:**
- Consumes: Task 1 (`trial_sender`, `trial_body`, constants), Task 3 (`is_verified_trial_phone`), `is_us`, `org_lock`.
- Produces:
  - `async def trial_texts_used(db, organization_id: UUID) -> int`: count of `Sms` rows with `organization_id`, `direction='outbound'`, `Sms.metadata_.has_key(SMS_TRIAL_META_KEY)` (matches `sms_trial_org_idx`), and `status != 'failed'`. A failed row never reached the phone and is not billed, so it does not use the cap.
  - `class TrialBodyTooLong(Exception)`: `max_chars: int` = `limit - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)`; `str(exc)` = `f"Trial texts can have up to {max_chars} characters. Nothing was sent."`
  - `@dataclass(frozen=True) class TrialSend: number: PhoneNumber; body: str; metadata: dict` (`metadata == {SMS_TRIAL_META_KEY: True}`)
  - `async def claim_trial_send(db, organization_id: UUID, *, to: str, body: str, limit: int, explicit_from: str | None = None, truncate: bool = False) -> TrialSend | None`

`claim_trial_send` returns `None` (call site keeps today's answer) unless all hold, checked in this order:
1. `number = await trial_sender(db)` is not `None` (configured and approved).
2. `organization_id != number.organization_id` (Hail's own workspace sends normally).
3. `explicit_from is None or explicit_from == number.e164`.
4. `is_us(to)`.
5. `await is_verified_trial_phone(db, organization_id, to)`.
6. `await org_lock(db, organization_id)`, then `trial_texts_used(...) < SMS_TRIAL_LIMIT`.
7. `trial_body(body, limit=limit, truncate=truncate)` is not `None`, else raise `TrialBodyTooLong`.

Do not call `us_registration_block(customer_org, trial_number, ...)`: it always blocks, because the number belongs to another org. `trial_sender` replaces it on the trial path.

The lock is transaction-scoped. Contract for every caller: insert the `Sms` row (with `TrialSend.metadata` merged last) and `commit` in the same transaction, with no commit in between. The commit releases the lock.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("case", ["off", "own_workspace", "other_from", "not_us",
    "not_verified_phone", "pending_phone", "other_workspace_phone", "number_not_approved", "cap_used"])
async def test_claim_refuses(case, ...):
    assert await claim_trial_send(s, org, to=..., body="hi", limit=1600, ...) is None

async def test_claim_allows_and_wraps(...):
    t = await claim_trial_send(s, org, to="+14155550142", body="hi", limit=1600)
    assert t.number.e164 == "+18005550199" and t.body == "[Hail trial] hi\nHail does not read replies."
    assert t.metadata == {"sms_trial": True}

async def test_failed_rows_do_not_use_the_cap(...):   # 24 sent + 3 failed -> trial_texts_used == 24, claim ok
async def test_25th_ok_26th_refused(...):

async def test_two_concurrent_claims_at_24_send_one(session_factory, ...):
    # seed 24 trial rows; two independent sessions run claim + insert Sms(trial) + commit via asyncio.gather
    # exactly one claim returns TrialSend; trial_texts_used == 25 afterwards
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v -k claim`
Expected: FAIL with `ImportError: claim_trial_send`

- [ ] **Step 3: Implement `trial_texts_used`, `TrialBodyTooLong`, `TrialSend`, `claim_trial_send`**

- [ ] **Step 4: Run to verify they pass**

Run: `cd core && uv run pytest tests/test_sms_trial.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/sms_trial.py core/tests/test_sms_trial.py
git commit -m "feat(sms): race-safe trial text claim"
```

---

### Task 5: Trial phone routes

**Files:**
- Create: `api/hailhq/api/routes/sms_trial.py` (`APIRouter(prefix="/sms/trial", tags=["sms"], responses=GENERAL_RATE_LIMITED_RESPONSES)`)
- Modify: `api/hailhq/api/main.py` (`_CUSTOMER_ROUTERS`: put `sms_trial_routes.router` **before** `sms_routes.router`, or `GET /sms/{sms_id}` captures `trial` and returns 422)
- Modify: `core/hailhq/core/schemas.py`
- Modify: `api/hailhq/api/deps.py` (`Principal` and `_principal_from_jwt`)
- Modify: `api/tests/conftest.py` (helpers below)
- Test: `api/tests/test_sms_trial_api.py`, `api/tests/test_auth_jwt.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4. `sms_route` from `hailhq.core.carrier_routing`. `write_audit_log`, `actor_of`.
- Produces (`hailhq.api.deps.Principal`): `console_session: bool = False`, set `True` on the JWT path only when `"azp" not in claims` (the same test the superadmin flag uses: MCP OAuth tokens are JWTs too and always carry `azp`). Test in `test_auth_jwt.py`: a token with `azp` gives `console_session False`; one without gives `True`.
- Produces (schemas):
  - `SmsTrialResponse`: `configured: bool` (`trial_number` found), `available: bool` (`trial_sender` found), `status: Literal["none","pending","verified"]`, `phone: str | None`, `code_expires_at: datetime | None`, `texts_used: int`, `texts_limit: int` (= 25), `texts_left: int`. Every field has a `description`.
  - `SmsTrialPhoneStart` (`extra="forbid"`): `phone: str` (E.164 validator), `recipient_consent: Literal[True]`, `consent_version: str` (1 to 40 chars).
  - `SmsTrialPhoneConfirm` (`extra="forbid"`): `code: str` (`pattern=r"^\d{6}$"`).
- Produces (routes, docstrings of 20+ characters for `test_openapi_descriptions.py`):
  - `GET /v1/sms/trial` → `SmsTrialResponse`. Any principal.
  - `POST /v1/sms/trial/phone` → 200 `SmsTrialResponse`. Console sessions only (`not principal.console_session` → 403 "Set up trial texting in the console."), because the consent tick and its wording live there. `trial_sender(db)` is `None` → 409 "Trial texting is not available yet." Maps `PhoneNotUs`/`PhoneIsHailNumber` → 422 (`loc=["body","phone"]`), `PhoneTaken` → 409, `CodeBudgetExceeded` → 429 with `Retry-After`. Sends `code_text(code)` with `sms_route(number.provider).send_sms(from_e164=number.e164, to_e164=phone, body=..., status_callback_url=None)`. Result `error_code == "21610"` → 422 "This phone replied STOP to Hail. Reply START, then try again."; other `error_code` → 422 "Hail could not text this phone. Check the number."; exception → 502 "Hail could not send the code. Try again." Commit only after an accepted send. Audit `sms.trial.code_sent` (payload: `phone` only, never the code).
  - `POST /v1/sms/trial/phone/confirm` → 200 `SmsTrialResponse`. Console sessions only. Maps `NoPendingCode`/`CodeExpired`/`WrongCode` → 422 (`loc=["body","code"]`), `PhoneTaken` → 409. Commit on success and after each wrong try (the try counter must persist). Audit `sms.trial.verified`.
  - Declare 409/422/429/502 in the decorators' `responses` so the spec shows them.
- Produces (test helpers in `api/tests/conftest.py`):
  - `async def seed_sms_trial(session, monkeypatch, *, e164: str = "+18005550199") -> PhoneNumber`: a new owner org, a toll-free `PhoneNumber` (`capabilities=["sms"]`, active, `provider="twilio"`), an approved toll-free `CarrierVerification` with `number_id`, `sms_registration_id` set, and `settings.hail_sms_trial_number` patched.
  - `async def verify_trial_phone(session, organization_id, phone: str = "+14155550142", *, status: str = "verified") -> SmsTrialPhone`
  - `async def seed_trial_texts(session, organization_id, n: int, *, status: str = "sent", to: str = "+14155550142") -> None`

- [ ] **Step 1: Write the failing tests**

```python
async def test_get_trial_off(client, org_and_key):           # setting empty -> configured False, available False, status "none", texts_left 25
async def test_get_trial_configured_not_approved(...):       # number seeded, no approval -> configured True, available False; POST phone -> 409, nothing sent
async def test_get_trial_route_not_captured_by_sms_id(client, ...):  # GET /v1/sms/trial is 200, not 422
async def test_start_needs_console_session(client, org_and_key):     # API key -> 403; JWT principal with console_session False -> 403
async def test_start_texts_a_code_from_the_trial_number(jwt_client, sms_mock, ...):
    call = sms_mock.send_sms.call_args.kwargs
    assert call["from_e164"] == "+18005550199" and call["to_e164"] == "+14155550142"
    assert re.search(r"code is \d{6}\. It expires in 10 minutes\. Reply STOP", call["body"])
    assert resp.json()["status"] == "pending"
async def test_code_text_is_not_an_sms_row_and_not_billed(...):      # no Sms row; no usage event; texts_used 0
async def test_start_maps_errors(...):  # parametrized: non-US 422, Hail number 422, taken 409, 6th code 429 + Retry-After
async def test_start_reports_stop_refusal(...):                      # sms_mock returns error_code "21610" -> 422, row not committed
async def test_confirm_flow(...):       # right code -> verified; wrong -> 422 "4 tries left"; expired -> 422
async def test_errors_never_name_a_carrier(...):                     # no "Twilio"/"Telnyx" in any detail above
```

Get a console principal the way `api/tests/test_contacts_api.py:535-550` does: `app.dependency_overrides[deps.get_current_principal] = lambda: deps.Principal(auth_kind="jwt", api_key_id=None, user_id=..., organization_id=org, scopes=["*"], console_session=True)`, popped in `finally`. Wrap it in a `jwt_client` fixture in the test file.

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && uv run pytest tests/test_sms_trial_api.py -v`
Expected: FAIL (404 on `/v1/sms/trial`)

- [ ] **Step 3: Implement the schemas, routes, router order, conftest helpers**

- [ ] **Step 4: Run to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_trial_api.py tests/test_openapi_descriptions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/sms_trial.py api/hailhq/api/main.py api/hailhq/api/deps.py core/hailhq/core/schemas.py api/tests/conftest.py api/tests/test_sms_trial_api.py api/tests/test_auth_jwt.py
git commit -m "feat(sms): trial phone routes"
```

---

### Task 6: Trial path in `POST /v1/sms`

**Files:**
- Modify: `api/hailhq/api/routes/sms.py:300-365` (`create_sms`)
- Modify: `core/hailhq/core/schemas.py` (`SmsCreate.metadata` reserved key, `SmsCreate.from_` description)
- Test: `api/tests/test_sms_trial_send.py`

**Interfaces:**
- Consumes: `claim_trial_send`, `TrialSend`, `TrialBodyTooLong`, `TRIAL_HINT`, `trial_enabled`, `SMS_TRIAL_META_KEY` (Tasks 1, 4); helpers from Task 5.
- Produces: `SmsCreate` rejects `metadata` containing `"sms_trial"` (422 "metadata keys are reserved: sms_trial"), mirroring the `CallCreate` reserved-key validator in `core/hailhq/core/schemas.py`. `from_` description adds one sentence: "A US workspace without an approved registration can omit from to send up to 25 trial texts to its verified trial phone."

Flow in `create_sms`, after `require_agent_send_allowed` and before the dedicated-number lookup:
- When `body.from_ is None or body.from_ == settings.hail_sms_trial_number`, and no registered number applies (`body.from_ is not None` or `not is_us(body.to)` or `registered_sms_number(...) is None`): call `claim_trial_send(db, org, to=body.to, body=body.body, limit=1600, explicit_from=body.from_)`. `TrialBodyTooLong` → 422 (`loc=["body","body"]`). A `TrialSend` sets `from_number`, `from_e164`, the wrapped body, and `metadata_ = {**body.metadata, **trial.metadata}`.
- `None` falls through to today's code. When `trial_enabled()` and `is_us(body.to)`, the two US 422s (no dedicated number; `us_registration_block`) end with `" " + TRIAL_HINT`.
- The `Sms` insert and `commit` stay right after the claim (Task 4 contract).

- [ ] **Step 1: Write the failing tests (one parametrized anti-goal table plus happy paths)**

```python
@pytest.mark.parametrize("case,expect", [
    ("not_verified_phone", 422), ("pending_phone", 422), ("other_workspace_phone", 422),
    ("cap_used_25", 422), ("explicit_own_unregistered_from", 422), ("feature_off", 422),
])
async def test_trial_send_refused(case, expect, client, sms_mock, ...):
    assert resp.status_code == expect
    assert all(c.kwargs["from_e164"] != "+18005550199" for c in sms_mock.send_sms.call_args_list)

async def test_explicit_own_from_422_names_the_trial(...):     # detail msg ends with TRIAL_HINT
async def test_trial_send_with_no_number(...):                  # org owns no number -> 201
    assert sent["body"] == "[Hail trial] hi\nHail does not read replies."
    assert row.from_e164 == "+18005550199" and row.metadata_["sms_trial"] is True
    assert usage event written for the row (billed like any outbound text)
async def test_from_equal_to_trial_number_uses_trial(...):
async def test_registered_number_preferred(...):                # approve_us_sms on own number -> sent from own, no prefix, texts_used 0
async def test_hail_own_workspace_sends_normally(...):          # owner org, from=trial -> body unchanged, no marker
async def test_trial_body_too_long_is_422(...):                 # 1600-char body -> 422, nothing sent
async def test_metadata_cannot_plant_marker(...):               # metadata {"sms_trial": True} -> 422
async def test_cap_counts_only_this_workspace(...):             # org B with 25 rows does not block org A
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && uv run pytest tests/test_sms_trial_send.py -v`
Expected: FAIL (`test_trial_send_with_no_number` gets 422)

- [ ] **Step 3: Implement the flow and the schema changes**

- [ ] **Step 4: Run to verify they pass, and that today's SMS tests still pass**

Run: `cd api && uv run pytest tests/test_sms_trial_send.py tests/test_sms_api.py tests/test_sms_sender_id_api.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/sms.py core/hailhq/core/schemas.py api/tests/test_sms_trial_send.py
git commit -m "feat(sms): trial texts from POST /v1/sms"
```

---

### Task 7: Trial path at the agent call sites

**Files:**
- Modify: `api/hailhq/api/routes/internal/agent.py`:
  - `_send_sms_to` (~324-441). Covers in-call sends on phone calls (`agent_send_sms` returns into it at ~636) and email-run member sends (`agent_email_run_send_sms`, ~1363).
  - `agent_send_sms` browser branch (~651-672, `transport == "browser"` only).
  - `agent_handover_sms` (~1935-2024).
- Test: `api/tests/test_internal_agent_trial.py` (reuse `_signed`, `_internal_secret`, `_insert_live_call` patterns from `api/tests/test_internal_agent_send.py`; `_seed`/`_post` from `api/tests/test_internal_agent_handover.py`)

**Interfaces:**
- Consumes: `claim_trial_send`, `TrialBodyTooLong` (Task 4); `SMS_MAX_BODY_CHARS` (480, already imported in `agent.py`); Task 5 helpers.
- Produces: `_SPOKEN_TRIAL_TOO_LONG_FMT = "Keep trial texts to {n} characters. Nothing was sent."` in `agent.py`.

At each site, when the normal sender is `None` (or cannot text) **or** `us_registration_block(...)` is not `None`, call `claim_trial_send(db, org, to=<recipient>, body=<words>, limit=SMS_MAX_BODY_CHARS, truncate=<see below>)`. A `TrialSend` replaces `from_number` and the body, merges `trial.metadata` last into `metadata_`, then the existing insert, commit, audit and `deliver_sms` run. `None` keeps today's answer (`_SPOKEN_SMS_UNCONFIGURED`, `_deny(... reason=block)`, `skipped:number_cannot_text`, `blocked:us_registration`).
- `_send_sms_to`: `truncate=False`; `TrialBodyTooLong` → `AgentSendResponse(ok=False, spoken=_SPOKEN_TRIAL_TOO_LONG_FMT.format(n=exc.max_chars))`. Pop `db.info["auto_bound_sms_number_id"]` before swapping the number, as today.
- Browser branch: skip the trial when `call.metadata_.get("browser_trial")` (the hail.so demo is out of scope). `truncate=False`, `limit=SMS_MAX_BODY_CHARS`. The 70-character check stays on the caller's own words, before the wrap. Claim after `reserve_browser_preview` and before the `Sms` insert: `reserve_browser_preview` takes the same `org_lock` (advisory locks re-enter in one transaction) and only flushes, so the lock holds until the insert commits. Heads-up: `browser_messaging.capabilities` decides whether a browser conversation offers texts at all, and this plan does not change it, so this branch is reached only when a number exists but is blocked.
- `agent_handover_sms`: keep the early `skipped:number_cannot_text` return unless `await is_verified_trial_phone(db, org, to)`. Only then fall through to the gates and the claim, so today's answers and `check_agent_send_allowed` logging stay the same for every other contact. `truncate=True` (the notice is cut today with `words[:SMS_MAX_BODY_CHARS]`; the footer must survive).

- [ ] **Step 1: Write the failing tests (one parametrized table per site)**

```python
SITES = ["in_call", "email_run", "handover", "browser"]

@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize("case", ["not_verified_phone", "pending_phone", "other_workspace_phone", "cap_used_25", "feature_off"])
async def test_agent_trial_refused(site, case, ...):
    # today's refusal for that site; no send_sms call with from_e164 == "+18005550199"

@pytest.mark.parametrize("site", SITES)
async def test_agent_trial_sent_with_prefix_and_footer(site, ...):
    # recipient == verified phone; org number unregistered (or none for in_call/email_run)
    assert sent["from_e164"] == "+18005550199"
    assert sent["body"].startswith("[Hail trial] ") and sent["body"].endswith("\nHail does not read replies.")
    assert row.metadata_["sms_trial"] is True

@pytest.mark.parametrize("site", SITES)
async def test_agent_registered_number_preferred(site, ...):  # approve_us_sms -> own number, no prefix

async def test_in_call_long_body_refused(...):     # 480-char body -> ok False, spoken "Keep trial texts to 440 characters..."
async def test_handover_long_notice_keeps_footer(...):  # body length == 480, ends with footer
async def test_handover_voice_only_number_still_skips_other_contacts(...):
    # call number has no "sms" capability, contact is not the verified phone -> "skipped:number_cannot_text", no AgentSendLog row
async def test_browser_demo_call_never_uses_trial(...): # call.metadata_ {"browser_trial": True}
async def test_reply_sms_never_uses_trial(...):         # /internal/agent/reply-sms to the verified phone keeps today's block
```

The in-call `max_chars` is `480 - len("[Hail trial] ") - len("\nHail does not read replies.")`; compute it from the constants in the test, do not hard-code it.

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && uv run pytest tests/test_internal_agent_trial.py -v`
Expected: FAIL (`test_agent_trial_sent_with_prefix_and_footer` refused at every site)

- [ ] **Step 3: Implement the three changes in `agent.py`**

- [ ] **Step 4: Run to verify they pass, and that today's agent tests still pass**

Run: `cd api && uv run pytest tests/test_internal_agent_trial.py tests/test_internal_agent_send.py tests/test_internal_agent_handover.py tests/test_internal_agent_reply.py tests/test_browser_messaging.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/internal/agent.py api/tests/test_internal_agent_trial.py
git commit -m "feat(agents): trial texts from agent sends and handover notices"
```

---

### Task 8: STOP refusals on the text record

Twilio toll-free numbers always handle STOP, START and UNSTOP; later texts fail with 21610 "asynchronously" (Twilio Advanced Opt-Out docs, read 2026-11-16). Today a create-time 21xxx is stored on the row (`TwilioSmsProvider.send_sms` returns it, `deliver_sms` saves `error_code`), but `receive_sms_status` ignores `ErrorCode`. A STOP to the trial number writes its suppression in Hail's own workspace, so the customer's own `check_sms_allowed` does not catch it: the carrier refusal is the only record.

**Files:**
- Modify: `api/hailhq/api/routes/sms.py` (`apply_sms_status`, ~429)
- Modify: `api/hailhq/api/routes/sms_webhooks/twilio.py` (`receive_sms_status`)
- Test: `api/tests/test_sms_api.py`, `api/tests/test_sms_trial_send.py`

**Interfaces:**
- Produces: `async def apply_sms_status(db, sms: Sms, new_status: str, *, error_code: str | None = None) -> None`. A non-`None` `error_code` is saved on `sms.error_code` (when it is empty), added to the `SmsEvent` payload, and to the terminal fan-out `data`. The Telnyx caller is unchanged.
- `receive_sms_status` passes `params.get("ErrorCode") or None` for `failed`/`undelivered`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_status_callback_stores_error_code(client, ...):
    # signed form MessageStatus=failed, ErrorCode=21610 -> row.status "failed", row.error_code "21610"
    # webhook data carries "error_code": "21610"
async def test_trial_text_refused_at_create_is_recorded_and_not_counted(...):
    # sms_mock returns error_code "21610" -> 201, status "failed", error_code "21610", texts_used unchanged
async def test_trial_text_refused_by_callback_frees_the_cap(...):
    # trial row sent, callback failed/21610 -> trial_texts_used drops by 1
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd api && uv run pytest tests/test_sms_api.py tests/test_sms_trial_send.py -v -k "error_code or refused"`
Expected: FAIL (`row.error_code is None`)

- [ ] **Step 3: Implement the `error_code` parameter and pass it from the Twilio callback**

- [ ] **Step 4: Run to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_api.py tests/test_sms_trial_send.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/sms.py api/hailhq/api/routes/sms_webhooks/twilio.py api/tests/test_sms_api.py api/tests/test_sms_trial_send.py
git commit -m "fix(sms): keep the carrier error code from status callbacks"
```

---

### Task 9: Contracts, docs, and production handover

**Files:**
- Modify: `openapi/openapi.yaml` (regenerated, never hand-edited)
- Modify: `cli/internal/client/client.gen.go` (regenerated), `cli/internal/cmd/sms.go` (`Long` help, ~35-45)
- Modify: `mcp/hailhq/mcp/tools.py` (`send_sms_tool` docstring, ~2482-2493)
- Modify: `docs/public/self-host/twilio.md` (new short section after "A2P 10DLC")
- Modify: `CHANGELOG.md` (`## [Unreleased]` → `### Added`)
- Modify (main checkout, not committed): `.env`

**Interfaces:**
- Consumes: everything above. No new names.

Exact copy:
- MCP `send_sms` docstring, replace the line "US long-code texting to US recipients requires ready A2P 10DLC registration and an attached number." with two lines:
  `US long-code texting to US recipients requires ready A2P 10DLC registration and an attached number.`
  `Without from_, a workspace without it can send up to 25 trial texts to its own verified phone, set up in the console. Each starts with "[Hail trial]".`
- CLI `hail sms` `Long`, add one paragraph: `A US workspace without an approved registration can omit --from to send up to 25 trial texts to its verified trial phone (console: Numbers > Trial texting).`
- `docs/public/self-host/twilio.md`, new section `**Trial texting (United States).**`: one paragraph: set `HAIL_SMS_TRIAL_NUMBER` to a toll-free number in the operator's own organization with approved toll-free verification; empty turns it off; set `HAIL_SMS_TRIAL_SENDING=true` once that verification covers trial texts; each organization verifies one phone and sends up to 25 texts; link `core/hailhq/core/sms_trial.py`.
- CHANGELOG: `- US trial texting: a workspace can send up to 25 texts to its own verified phone before its US registration is approved. Set it up in the console under Numbers.`

- [ ] **Step 1: Regenerate the spec (API running on :8080 against a migrated local DB)**

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
```

Expected: diff shows `/v1/sms/trial`, `/v1/sms/trial/phone`, `/v1/sms/trial/phone/confirm`, the three schemas, and the `SmsCreate` description changes only.

- [ ] **Step 2: Regenerate the CLI client and edit the help text**

```bash
cd cli
make codegen
go test ./...
```

Expected: PASS

- [ ] **Step 3: Edit the MCP docstring, docs and CHANGELOG; run the contract tests**

```bash
cd mcp && uv run pytest -q
cd .. && pnpm postman:test
```

Expected: PASS (`mcp/tests/test_schema_matches_api.py` and `test_tool_annotations.py` included)

- [ ] **Step 4: Full suite, lint, spec gate**

```bash
uv run --directory api pytest -q
uv run --directory core pytest -q
uv run ruff check . && uv run black --check .
```

Then run the `openapi-check.yml` script body locally. Expected: `openapi.yaml gate OK`.

- [ ] **Step 5: `.env` files**

Value: the real E.164 of Hail's toll-free number. Read it from production: the active `toll_free` row of Hail's own workspace in `phone_numbers`.

1. Rebase the branch on `origin/main` first, so its `.env.example` has every key `main` has.
2. Worktree `.env`: a copy of the main checkout `.env` plus the Task 1 lines at the same place, with the real value. From the worktree root, this must print nothing:

```bash
diff <(sed -E 's/^([A-Z][A-Z0-9_]*)=.*/\1=/' .env) \
  <(sed -E 's/^([A-Z][A-Z0-9_]*)=.*/\1=/' .env.example)
```

3. Main checkout `.env`: add the same lines at the same place, with the real value, so it is ready for the VM at merge. Its diff against the main checkout `.env.example` prints only the new key until the PR merges, then nothing. Run it again after merge.

- [ ] **Step 6: Check the trial number has no text agent**

The trial number must not answer replies ("Hail does not read replies"). Read its `phone_numbers.sms_agent_id` in production; it must be NULL. If it is set, stop and ask the owner before merge.

- [ ] **Step 7: Commit and open the PR**

```bash
git add openapi/openapi.yaml cli/internal/client/client.gen.go cli/internal/cmd/sms.go mcp/hailhq/mcp/tools.py docs/public/self-host/twilio.md CHANGELOG.md
git commit -m "docs(sms): trial texting contracts and docs"
```

PR body: what merge does (GHA deploy on push to `main`, Alembic migration on deploy), what it does not do (the VM `.env` key, the toll-free verification). Generic steps only; the exact number and VM steps go in the chat reply.

**After merge (owner, from the chat reply):**
1. Copy the main checkout `.env` to the VM as is; restart the `api` service.
2. No code or trial text goes out until the trial number's toll-free verification is approved (`trial_sender`). Before that, the console card shows the form with sending turned off, so the owner can screenshot the opt-in step for the filing. File the combined use case (account notices and trial texts) from that screenshot (spec, "Toll-free verification").
3. Merge the hail-website console PR only after this deploy finishes.

---

## Self-Review

1. **Spec coverage:** setting (T1), table (T2), proof (T3), cap and helper (T4), console endpoints (T5), public send before the 422 (T6), agent sites (T7), STOP refusal on the record (T8), OpenAPI, MCP, CLI, docs, CHANGELOG, `.env` (T9). Inbound: nothing new, by design.
2. **Anti-goals at every call site:** non-verified phone, other workspace's phone, prefix/footer, 26th text: T6 table and T7 tables. Explicit own `from`: T6 (public API only; agents never pick a `from`).
3. **Names:** `claim_trial_send`, `TrialSend`, `TrialBodyTooLong`, `trial_texts_used`, `trial_body`, `trial_number`, `trial_sender`, `is_verified_trial_phone`, `Principal.console_session`, `SMS_TRIAL_META_KEY`, `SmsTrialPhone`, `seed_sms_trial`, `verify_trial_phone`, `seed_trial_texts`, `apply_sms_status(error_code=)` are each defined once, in an earlier task than their first use.
4. **Not covered, by decision:** a per-phone cap across workspaces on code texts (`forward_targets` has `MAX_CONFIRM_MAILS_PER_ADDRESS_PER_DAY`); DSAR erasure of `sms_trial_phones` (`core/hailhq/core/dsar.py` does not cover `email_forward_targets` either). Both are follow-ups for the owner to decide.
5. **Known behavior, unchanged:** `require_funds` runs before the trial path, so a workspace with no credit gets the funds refusal. A STOP to the trial number is stored in Hail's own workspace, and the abuse monitor counts it there (it skips orgs under `HAIL_SMS_ABUSE_MIN_SENDS` sends a day).
