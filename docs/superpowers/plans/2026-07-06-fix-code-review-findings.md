# Fix Code-Review Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 7 confirmed findings from the `/code-review` pass on pending, uncommitted changes in `/Users/r/hail` (a Python monorepo: `api/`, `core/`, `mcp/`, `voicebot/`, `cli/`, `sdk/`), without changing any other intended behavior.

**Architecture:** Two findings (SDK, CLI) fix client integrations that the in-flight API change broke; two (DSAR case-sensitivity, DSAR cc/bcc) fix the same `lookup_recipient` function in sequence; two (org_closures race, org_closures dependency wiring) fix the same small route file in sequence; one (HMAC triplication) extracts a shared module used by three existing files. Tasks within a shared file are ordered so each builds on the prior task's version; tasks across different files are independent.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async, Postgres), pytest + pytest-asyncio, Pydantic v2 (API + SDK), Go 1.26 (CLI, oapi-codegen-generated client), Cobra (CLI framework).

## Global Constraints

- Python tests: `pytest` (repo uses per-package `pyproject.toml`s — run from within `api/`, `core/`, `mcp/`, or `voicebot/` via `uv run pytest`). Async tests use `pytest-asyncio`; API-level tests use `@pytest.mark.asyncio` explicitly (see existing `api/tests/test_internal_org_closures.py`), core-level tests rely on `asyncio_mode` config (no decorator needed — match whichever convention the file you're editing already uses).
- Go tests: `cd cli && go test ./...`. Go build: `cd cli && go build ./...`.
- Verify every Python task with `uv run pytest` (full run of the affected package) plus `uv run ruff check <touched files>` — both must be clean before moving to the next task.
- Do not touch `openapi/openapi.yaml` by hand — it is generated and was already regenerated as part of the in-flight diff this plan fixes findings for.
- Do not invent new business/consent-policy behavior — every fix here restores parity with `core/hailhq/core/schemas.py`'s already-decided contract (required `recipient_consent`, optional `consent_source`/`consent_obtained_at`, `message_type` defaulting to `"informational"`), not a new design.

---

### Task 1: Add consent fields to the Python SDK

**Files:**

- Modify: `sdk/hail/models.py:72-107` (`CallCreate`), `sdk/hail/models.py:184-235` (`EmailCreate`)
- Modify: `sdk/hail/client.py:69-106` (`_CallsResource.create`), `sdk/hail/client.py:134-187` (`_EmailsResource.create`)
- Modify: `sdk/tests/test_client.py` (existing `.calls.create(...)` call sites), `sdk/tests/test_emails.py` (existing `.emails.create(...)` call sites)
- Test: `sdk/tests/test_client.py`, `sdk/tests/test_emails.py` (new assertions alongside the existing-call-site fixes)

**Interfaces:**

- Consumes: nothing new from other tasks.
- Produces: `CallCreate`/`EmailCreate` gain `recipient_consent: bool` (required), `consent_source: str | None = None`, `consent_obtained_at: datetime | None = None`, `message_type: Literal["marketing", "informational"] = "informational"` — matching `core/hailhq/core/schemas.py` exactly. `_CallsResource.create`/`_EmailsResource.create` gain the same 4 as keyword-only params, `recipient_consent` required (no default), the rest optional.

**Root cause:** `core/hailhq/core/schemas.py`'s `CallCreate`/`EmailCreate` gained a required `recipient_consent` field (plus 3 related fields) in the in-flight API diff. `sdk/hail/models.py`'s hand-maintained mirror was not updated, and `sdk/hail/client.py`'s `create()` methods build the request body from a fixed keyword signature with no way to pass these fields at all. Every SDK-based call/email creation now either raises a `TypeError` (if a caller tries to guess a kwarg) or gets a 422 from the live API (normal usage).

- [ ] **Step 1: Write the failing test**

Add to `sdk/tests/test_client.py`, right after `test_calls_create_happy_path_mode_a` (which currently asserts `body == {"to": ..., "system_prompt": ...}` with no consent fields):

```python
@respx.mock
async def test_calls_create_sends_consent_fields(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            recipient_consent=True,
            consent_source="signup_form",
            message_type="marketing",
        )
    body = json.loads(route.calls.last.request.content)
    assert body["recipient_consent"] is True
    assert body["consent_source"] == "signup_form"
    assert body["message_type"] == "marketing"


@respx.mock
async def test_calls_create_omits_optional_consent_fields_when_not_passed(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(
            to="+15555550123", system_prompt="be polite", recipient_consent=True
        )
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "to": "+15555550123",
        "system_prompt": "be polite",
        "recipient_consent": True,
    }
```

Add to `sdk/tests/test_emails.py`, near its existing `.emails.create(...)` tests (match that file's existing import/fixture style):

```python
@respx.mock
async def test_emails_create_sends_consent_fields(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/emails").mock(
        return_value=httpx.Response(201, json=make_email_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.emails.create(
            to=["a@example.com"],
            subject="hi",
            body_text="hello",
            recipient_consent=True,
            consent_source="signup_form",
            message_type="marketing",
        )
    body = json.loads(route.calls.last.request.content)
    assert body["recipient_consent"] is True
    assert body["consent_source"] == "signup_form"
    assert body["message_type"] == "marketing"
```

(`make_email_response` should already exist in `sdk/tests/conftest.py` alongside `make_call_response` — if the exact helper name differs, use whatever this file's other passing tests already use to mock a successful `/emails` response.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd sdk && uv run pytest tests/test_client.py::test_calls_create_sends_consent_fields tests/test_emails.py::test_emails_create_sends_consent_fields -v`
Expected: FAIL — `TypeError: create() got an unexpected keyword argument 'recipient_consent'`

- [ ] **Step 3: Add the fields to `sdk/hail/models.py`**

In `CallCreate` (currently lines 72-107), add after the `metadata` field (line 88):

```python
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"
```

In `EmailCreate` (currently lines 184-235), add after the `metadata` field (line 206):

```python
    recipient_consent: bool
    consent_source: str | None = None
    consent_obtained_at: datetime | None = None
    message_type: Literal["marketing", "informational"] = "informational"
```

(`Literal` and `datetime` are already imported at the top of `sdk/hail/models.py` — no new imports needed.)

- [ ] **Step 4: Add the fields to `sdk/hail/client.py`**

In `_CallsResource.create` (lines 69-106), change the signature and body-building:

```python
    async def create(
        self,
        *,
        to: str,
        recipient_consent: bool,
        system_prompt: str | None = None,
        llm: LLMConfig | dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        consent_source: str | None = None,
        consent_obtained_at: datetime | None = None,
        message_type: Literal["marketing", "informational"] | None = None,
        idempotency_key: str | None = None,
    ) -> CallResponse:
        """Originate an outbound call.

        Exactly one of ``system_prompt`` (mode A) or a fully-populated
        ``llm`` block (mode B) must be provided — server enforces this with
        a 422; we don't pre-validate so SDK and API stay in lockstep on the
        rule. ``recipient_consent`` is required — the server 422s without
        it. ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        body: dict[str, Any] = {"to": to, "recipient_consent": recipient_consent}
        if from_ is not None:
            body["from"] = from_
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if first_message is not None:
            body["first_message"] = first_message
        if metadata is not None:
            body["metadata"] = metadata
        if llm is not None:
            body["llm"] = llm.model_dump() if isinstance(llm, LLMConfig) else llm
        if consent_source is not None:
            body["consent_source"] = consent_source
        if consent_obtained_at is not None:
            body["consent_obtained_at"] = consent_obtained_at.isoformat()
        if message_type is not None:
            body["message_type"] = message_type
```

(the rest of the method — `key = idempotency_key or ...` through the `return` — is unchanged.)

In `_EmailsResource.create` (lines 134-187), change the signature and body-building the same way:

```python
    async def create(
        self,
        *,
        to: list[str],
        subject: str,
        recipient_consent: bool,
        body_text: str | None = None,
        body_html: str | None = None,
        from_: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        conversation_id: UUID | str | None = None,
        metadata: dict[str, Any] | None = None,
        consent_source: str | None = None,
        consent_obtained_at: datetime | None = None,
        message_type: Literal["marketing", "informational"] | None = None,
        idempotency_key: str | None = None,
    ) -> EmailResponse:
        """Send an outbound email.

        At least one of ``body_text`` / ``body_html`` is required; the
        server returns 422 if neither is supplied. ``recipient_consent``
        is required — the server 422s without it. ``from_`` is optional:
        when omitted the server picks the first verified sender on the
        org or auto-mints a hail-mail address (operator-configured).
        ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        body: dict[str, Any] = {
            "to": list(to),
            "subject": subject,
            "recipient_consent": recipient_consent,
        }
        if from_ is not None:
            body["from"] = from_
        if body_text is not None:
            body["body_text"] = body_text
        if body_html is not None:
            body["body_html"] = body_html
        if cc:
            body["cc"] = list(cc)
        if bcc:
            body["bcc"] = list(bcc)
        if reply_to is not None:
            body["reply_to"] = reply_to
        if conversation_id is not None:
            body["conversation_id"] = str(conversation_id)
        if metadata is not None:
            body["metadata"] = metadata
        if consent_source is not None:
            body["consent_source"] = consent_source
        if consent_obtained_at is not None:
            body["consent_obtained_at"] = consent_obtained_at.isoformat()
        if message_type is not None:
            body["message_type"] = message_type
```

(the rest of the method is unchanged. `Literal` is already imported at the top of `sdk/hail/client.py`.)

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `cd sdk && uv run pytest tests/test_client.py::test_calls_create_sends_consent_fields tests/test_client.py::test_calls_create_omits_optional_consent_fields_when_not_passed tests/test_emails.py::test_emails_create_sends_consent_fields -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Fix every pre-existing call site (now missing a required kwarg)**

10 existing calls to `.calls.create(...)`/`.emails.create(...)` across `sdk/tests/test_client.py` and `sdk/tests/test_emails.py` don't pass `recipient_consent` and will now fail with `TypeError: create() missing 1 required keyword-only argument`. Fix each exactly as follows (do not touch the 3 new tests Step 1 already added):

In `sdk/tests/test_client.py`, `test_calls_create_happy_path_mode_a` (around line 55) — this one also has an exact-dict body assertion that must be updated in the same edit:

```python
        call = await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            recipient_consent=True,
            idempotency_key="idem-fixed",
        )
```

and further down in the same test:

```python
    assert body == {
        "to": "+15555550123",
        "system_prompt": "be polite",
        "recipient_consent": True,
    }
```

`test_calls_create_auto_generates_idempotency_key` (around line 76):

```python
        await c.calls.create(to="+15555550123", system_prompt="be polite", recipient_consent=True)
```

`test_calls_create_propagates_explicit_idempotency_key` (around line 89):

```python
        await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            recipient_consent=True,
            idempotency_key="caller-supplied",
        )
```

`test_calls_create_with_llm_block` (around line 103):

```python
        await c.calls.create(
            to="+15555550123",
            recipient_consent=True,
            llm={
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "model": "gpt-4o-mini",
            },
```

(leave the rest of that call's closing `)` and everything after it as-is)

`test_error_mapping_422` (around line 392) and `test_error_mapping_409` (around line 404) — both currently read `await c.calls.create(to="+15555550123", system_prompt="hi")`; change each to:

```python
            await c.calls.create(to="+15555550123", system_prompt="hi", recipient_consent=True)
```

In `sdk/tests/test_emails.py`, the first test (around line 27):

```python
        email = await c.emails.create(
            to=["recipient@example.com"],
            subject="test subject",
            body_text="test body",
            recipient_consent=True,
            idempotency_key="idem-fixed",
        )
```

The test at line 56 (currently `await c.emails.create(to=["x@example.com"], subject="hi", body_text="body")`):

```python
        await c.emails.create(to=["x@example.com"], subject="hi", body_text="body", recipient_consent=True)
```

The test at line 67:

```python
        await c.emails.create(
            to=["x@example.com"],
            subject="hi",
            body_text="body",
            recipient_consent=True,
            from_="alerts@acme.com",
        )
```

The test at line 84:

```python
        await c.emails.create(
            to=["a@example.com"],
            subject="hi",
            body_text="body",
            recipient_consent=True,
            cc=["b@example.com"],
            bcc=["c@example.com"],
            reply_to="replyto@example.com",
        )
```

After making all these edits, run: `cd sdk && grep -rn "\.calls\.create(\|\.emails\.create(" tests/*.py` and confirm every result's surrounding call now has `recipient_consent=True` somewhere in it (cross-check against the 10 line numbers above plus the 3 new tests from Step 1 — 13 total call sites, all with `recipient_consent`).

- [ ] **Step 7: Run the full SDK suite**

Run: `cd sdk && uv run pytest -q`
Expected: all tests pass, 0 failures (confirms every pre-existing call site was fixed in Step 6)

- [ ] **Step 8: Lint**

Run: `cd sdk && uv run ruff check .`
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add sdk/hail/models.py sdk/hail/client.py sdk/tests/test_client.py sdk/tests/test_emails.py
git commit -m "fix(sdk): add recipient_consent/consent_source/consent_obtained_at/message_type to CallCreate/EmailCreate"
```

---

### Task 2: Regenerate the Go CLI client and add consent flags

**Files:**

- Regenerate: `cli/internal/client/client.gen.go` (via `make codegen`, not hand-edited)
- Modify: `cli/internal/cmd/call.go` (flags struct + `runCall`), `cli/internal/cmd/email.go` (flags struct + `runEmailSend`)
- Test: `cli/internal/cmd/call_test.go`, `cli/internal/cmd/email_test.go`

**Interfaces:**

- Consumes: nothing from Task 1 (SDK and CLI are independent clients of the same API).
- Produces: `hail call` gains `--recipient-consent`, `--consent-source`, `--consent-obtained-at`, `--message-type` flags; `hail email send` gains the same 4. Both wire into the (regenerated) `client.CallCreate`/`client.EmailCreate` structs.

**Root cause:** `openapi/openapi.yaml` was already regenerated (in the diff this plan fixes findings for) to include the new consent fields, but `cli/internal/client/client.gen.go` — the CLI's committed, codegen'd Go client — was never regenerated from it, and `call.go`/`email.go` have no flags to supply the fields even after regeneration. Every `hail call`/`hail email send` invocation 422s against the live (already-updated) API with no client-side way to comply.

- [ ] **Step 1: Regenerate the client and inspect the new field names**

Run: `cd cli && make codegen`
Expected: exits 0, `internal/client/client.gen.go` is rewritten.

Run: `cd cli && grep -n "RecipientConsent\|ConsentSource\|ConsentObtainedAt\|MessageType" internal/client/client.gen.go`
Expected: shows the exact generated field names/types on `CallCreate` and `EmailCreate` (they should be `RecipientConsent bool`, `ConsentSource *string`, `ConsentObtainedAt *time.Time`, and `MessageType` as either `*string` or a generated enum type like `*CallCreateMessageType` — read the actual output and use those exact names in Steps 3-4 below; if a name differs from what's written there, use the real generated name instead).

Run: `cd cli && go build ./...`
Expected: exits 0 (confirms the regenerated client still compiles cleanly against the rest of the CLI)

- [ ] **Step 2: Write the failing tests**

Add to `cli/internal/cmd/call_test.go`, near the other `TestCallSubcommand_*` tests:

```go
func TestCallSubcommand_SendsConsentFlags(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567",
		"--prompt", "you are a polite agent",
		"--recipient-consent",
		"--consent-source", "signup_form",
		"--message-type", "marketing",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("bad request body: %v", err)
	}
	if body["recipient_consent"] != true {
		t.Errorf("recipient_consent = %v, want true", body["recipient_consent"])
	}
	if body["consent_source"] != "signup_form" {
		t.Errorf("consent_source = %v, want signup_form", body["consent_source"])
	}
	if body["message_type"] != "marketing" {
		t.Errorf("message_type = %v, want marketing", body["message_type"])
	}
}

func TestCallSubcommand_OmitsRecipientConsentWhenNotPassed(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"call", "+15551234567", "--prompt", "you are a polite agent",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("bad request body: %v", err)
	}
	if _, ok := body["recipient_consent"]; ok {
		t.Errorf("recipient_consent should be omitted when --recipient-consent not passed, got %v", body["recipient_consent"])
	}
}
```

Add the analogous pair to `cli/internal/cmd/email_test.go` (match its existing test naming/helper style — it should already have its own `newFakeServer`-equivalent or reuse `call_test.go`'s since they're the same package `cmd`):

```go
func TestEmailSendSubcommand_SendsConsentFlags(t *testing.T) {
	srv := newFakeServer(t, http.StatusCreated, sampleEmailResponse())

	_, _, err := runRoot(t,
		map[string]string{"HAIL_API_KEY": "sk_test", "HAIL_API_URL": srv.URL},
		"email", "send",
		"--to", "a@example.com",
		"--subject", "hi",
		"--body", "hello",
		"--recipient-consent",
		"--consent-source", "signup_form",
		"--message-type", "marketing",
	)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	var body map[string]any
	if err := json.Unmarshal(srv.lastBody, &body); err != nil {
		t.Fatalf("bad request body: %v", err)
	}
	if body["recipient_consent"] != true {
		t.Errorf("recipient_consent = %v, want true", body["recipient_consent"])
	}
	if body["consent_source"] != "signup_form" {
		t.Errorf("consent_source = %v, want signup_form", body["consent_source"])
	}
}
```

(`sampleEmailResponse` should already exist in `email_test.go` for the pre-existing happy-path tests — if the exact helper name differs, use whatever those existing tests already call.)

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd cli && go test ./internal/cmd/... -run 'TestCallSubcommand_SendsConsentFlags|TestCallSubcommand_OmitsRecipientConsentWhenNotPassed|TestEmailSendSubcommand_SendsConsentFlags' -v`
Expected: FAIL — `unknown flag: --recipient-consent`

- [ ] **Step 4: Add the flags to `call.go`**

In `cli/internal/cmd/call.go`, extend the `callFlags` struct (currently lines 18-26):

```go
type callFlags struct {
	prompt           string
	llmURL           string
	llmKey           string
	llmModel         string
	from             string
	firstMessage     string
	idempotencyKey   string
	recipientConsent bool
	consentSource    string
	consentObtainedAt string
	messageType      string
}
```

Register the new flags after the existing `cmd.Flags()...` block (after line 70):

```go
	cmd.Flags().BoolVar(&f.recipientConsent, "recipient-consent", false, "Confirm the recipient has consented to receive this call (required by the API)")
	cmd.Flags().StringVar(&f.consentSource, "consent-source", "", "Where/how consent was obtained (required if --message-type=marketing)")
	cmd.Flags().StringVar(&f.consentObtainedAt, "consent-obtained-at", "", "RFC 3339 timestamp consent was obtained at (optional)")
	cmd.Flags().StringVar(&f.messageType, "message-type", "", "\"marketing\" or \"informational\" (default: informational)")
```

In `runCall` (currently lines 79-118), after the `body := client.CallCreate{...}` block and before `if f.llmURL != ""`, add:

```go
	body.RecipientConsent = f.recipientConsent
	if f.consentSource != "" {
		body.ConsentSource = strPtr(f.consentSource)
	}
	if f.consentObtainedAt != "" {
		t, err := time.Parse(time.RFC3339, f.consentObtainedAt)
		if err != nil {
			return fmt.Errorf("--consent-obtained-at: invalid RFC 3339 timestamp: %w", err)
		}
		body.ConsentObtainedAt = &t
	}
	if f.messageType != "" {
		mt := client.CallCreateMessageType(f.messageType)
		body.MessageType = &mt
	}
```

(Adjust `client.CallCreateMessageType` to whatever enum/type name Step 1's grep actually showed — if `MessageType` generated as a plain `*string` instead of an enum type, use `body.MessageType = &f.messageType` directly instead.)

Add `"time"` to the import block at the top of `call.go` (it currently imports `context`, `encoding/json`, `fmt`, `net/http`, `strings` — add `"time"` alongside them).

- [ ] **Step 5: Add the flags to `email.go`**

In `cli/internal/cmd/email.go`, extend `emailSendFlags` (currently lines 18-30):

```go
type emailSendFlags struct {
	to                []string
	cc                []string
	bcc               []string
	from              string
	replyTo           string
	subject           string
	body              string
	bodyHTML          string
	bodyFile          string
	bodyHTMLFile      string
	idempotencyKey    string
	recipientConsent  bool
	consentSource     string
	consentObtainedAt string
	messageType       string
}
```

Register the new flags after the existing `cmd.Flags()...` block (after line 95):

```go
	cmd.Flags().BoolVar(&f.recipientConsent, "recipient-consent", false, "Confirm the recipient has consented to receive this email (required by the API)")
	cmd.Flags().StringVar(&f.consentSource, "consent-source", "", "Where/how consent was obtained (required if --message-type=marketing)")
	cmd.Flags().StringVar(&f.consentObtainedAt, "consent-obtained-at", "", "RFC 3339 timestamp consent was obtained at (optional)")
	cmd.Flags().StringVar(&f.messageType, "message-type", "", "\"marketing\" or \"informational\" (default: informational)")
```

In `runEmailSend` (currently lines 100-169), after the `body := client.EmailCreate{...}` block's `if len(bcc) > 0 { body.Bcc = &bcc }`, add:

```go
	body.RecipientConsent = f.recipientConsent
	if f.consentSource != "" {
		body.ConsentSource = strPtr(f.consentSource)
	}
	if f.consentObtainedAt != "" {
		t, err := time.Parse(time.RFC3339, f.consentObtainedAt)
		if err != nil {
			return fmt.Errorf("--consent-obtained-at: invalid RFC 3339 timestamp: %w", err)
		}
		body.ConsentObtainedAt = &t
	}
	if f.messageType != "" {
		mt := client.EmailCreateMessageType(f.messageType)
		body.MessageType = &mt
	}
```

(Same note as Step 4 — adjust `client.EmailCreateMessageType` to the real generated type name.)

Add `"time"` to `email.go`'s import block (currently `context`, `fmt`, `net/http`, `os`, `strings`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd cli && go test ./internal/cmd/... -run 'TestCallSubcommand_SendsConsentFlags|TestCallSubcommand_OmitsRecipientConsentWhenNotPassed|TestEmailSendSubcommand_SendsConsentFlags' -v`
Expected: PASS (3 tests)

- [ ] **Step 7: Run the full CLI test suite and build**

Run: `cd cli && go test ./... && go build ./... && go vet ./...`
Expected: all pass, 0 failures, clean build, no vet warnings

- [ ] **Step 8: Commit**

```bash
git add cli/internal/client/client.gen.go cli/internal/cmd/call.go cli/internal/cmd/email.go cli/internal/cmd/call_test.go cli/internal/cmd/email_test.go
git commit -m "fix(cli): regenerate client from updated openapi.yaml, add consent flags to call/email send"
```

---

### Task 3: Fix the org-closures race condition with an atomic upsert

**Files:**

- Modify: `api/hailhq/api/routes/internal/org_closures.py:41-63` (`record_org_closure`)
- Test: `api/tests/test_internal_org_closures.py`

**Interfaces:**

- Consumes: `OrgClosure` model (`core/hailhq/core/models.py`, unchanged), `OrgClosureIn` (same file, unchanged).
- Produces: `record_org_closure(body: OrgClosureIn, db: AsyncSession) -> dict` — same signature and return shape as today; now safe under concurrent calls for the same `organization_id`.

**Root cause:** `record_org_closure` does `existing = await db.get(OrgClosure, body.organization_id)` then either updates or `db.add()`s a new row, with no locking or `ON CONFLICT` handling. Two concurrent requests for the same org can both see `existing is None` and both attempt an insert; the second commit raises an unhandled `IntegrityError` (surfacing as a 500), contradicting the module's documented idempotency guarantee.

- [ ] **Step 1: Write the failing test**

Add to `api/tests/test_internal_org_closures.py` (add `import asyncio` and `from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession` — `AsyncSession` is already imported — to the top, plus a direct import of the route function):

```python
from hailhq.api.routes.internal.org_closures import OrgClosureIn, record_org_closure
```

Then add this test at the end of the file:

```python
async def test_concurrent_notifications_for_same_org_do_not_race(
    session_factory: async_sessionmaker[AsyncSession],
):
    """Two concurrent notifications for the same organization_id must not
    raise an IntegrityError — the whole point of the documented
    idempotency guarantee. Uses two independent sessions (like two real
    concurrent requests would get), not the shared-session `client`
    fixture, so this actually exercises the race."""
    org_id = uuid.uuid4()
    closed_at = datetime.now(timezone.utc).replace(microsecond=0)

    async def _notify() -> dict:
        async with session_factory() as session:
            return await record_org_closure(
                OrgClosureIn(organization_id=org_id, closed_at=closed_at, source="hail_website"),
                session,
            )

    results = await asyncio.gather(_notify(), _notify())
    assert all(r["organization_id"] == str(org_id) for r in results)
```

- [ ] **Step 2: Run test to verify it fails (or is flaky)**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py::test_concurrent_notifications_for_same_org_do_not_race -v`
Expected: FAILS with an unhandled `sqlalchemy.exc.IntegrityError` (may take a couple of runs to reproduce if the two coroutines happen not to interleave at the exact right point — if it passes on the first try, run it 5 times in a loop to confirm: `for i in 1 2 3 4 5; do uv run pytest tests/test_internal_org_closures.py::test_concurrent_notifications_for_same_org_do_not_race -v; done`)

- [ ] **Step 3: Implement the atomic upsert**

Replace `api/hailhq/api/routes/internal/org_closures.py`'s imports and `record_org_closure` body:

```python
from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.db import get_session
from hailhq.core.models import OrgClosure

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


class OrgClosureIn(BaseModel):
    organization_id: UUID
    closed_at: datetime
    source: str = "hail_website"


@router.post("/org-closures", dependencies=[Depends(verify_internal_request)])
async def record_org_closure(
    body: OrgClosureIn,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    stmt = (
        pg_insert(OrgClosure)
        .values(
            organization_id=body.organization_id,
            closed_at=body.closed_at,
            source=body.source,
        )
        .on_conflict_do_update(
            index_elements=["organization_id"],
            set_={"closed_at": body.closed_at, "source": body.source},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return {
        "organization_id": str(body.organization_id),
        "closed_at": body.closed_at.isoformat(),
        "source": body.source,
    }
```

(This task does not touch the router-level-vs-per-route dependency wiring — that's Task 4.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py::test_concurrent_notifications_for_same_org_do_not_race -v`
Expected: PASS, consistently across 5 runs.

- [ ] **Step 5: Run the full org-closures + retention suites**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py -v`
Expected: all pass, including the pre-existing `test_happy_path_inserts_row` and `test_repeat_notification_upserts_existing_row` (the upsert must preserve identical externally-visible behavior for the non-racing cases)

- [ ] **Step 6: Lint**

Run: `cd api && uv run ruff check hailhq/api/routes/internal/org_closures.py tests/test_internal_org_closures.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add api/hailhq/api/routes/internal/org_closures.py api/tests/test_internal_org_closures.py
git commit -m "fix(api): make org-closure notification an atomic upsert, not read-then-write"
```

---

### Task 4: Move org-closures' auth dependency to router level

**Files:**

- Modify: `api/hailhq/api/routes/internal/org_closures.py:32,41` (router construction + route decorator)

**Interfaces:**

- Consumes: `verify_internal_request` (unchanged in this task — Task 7 changes its internals, not its signature).
- Produces: no interface change — same dependency, attached at the router instead of the route.

**Root cause:** `dsar.py`'s router is constructed with `dependencies=[Depends(verify_internal_request)]` at the `APIRouter(...)` level, so any future route added to it is auto-protected. `org_closures.py` instead attaches the same dependency per-route on the single existing `@router.post(...)` decorator — harmless today (there's only one route), but a future second endpoint in this file would ship unauthenticated by default unless its author remembers to copy the dependency.

- [ ] **Step 1: Write the failing test**

The existing `test_rejects_missing_signature` and `test_rejects_bad_signature` tests in `api/tests/test_internal_org_closures.py` already prove the _current_ route is protected — they'll keep passing regardless of whether the dependency is router-level or per-route, so they don't catch this specific issue. Add a new test that would catch a _regression_ (a hypothetical second unprotected route) by asserting the guarantee at the router-construction level instead:

```python
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.internal.org_closures import router as org_closures_router


def test_router_level_dependency_protects_the_whole_router():
    """Mirrors dsar.py's router-level wiring: the auth dependency must be
    attached to the APIRouter itself, not to individual route decorators,
    so a future route added to this file is protected by construction."""
    dependant_callables = {
        dep.call for route in org_closures_router.routes for dep in route.dependencies
    }
    assert verify_internal_request in dependant_callables
    router_level_dependant_callables = {
        dep.call for dep in org_closures_router.dependencies
    }
    assert verify_internal_request in router_level_dependant_callables
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py::test_router_level_dependency_protects_the_whole_router -v`
Expected: FAIL — `verify_internal_request` is not in `org_closures_router.dependencies` (it's only on the individual route)

- [ ] **Step 3: Move the dependency to the router**

In `api/hailhq/api/routes/internal/org_closures.py`, change:

```python
router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)
```

to:

```python
router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)
```

and change the route decorator from:

```python
@router.post("/org-closures", dependencies=[Depends(verify_internal_request)])
```

to:

```python
@router.post("/org-closures")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py::test_router_level_dependency_protects_the_whole_router -v`
Expected: PASS

- [ ] **Step 5: Run the full org-closures suite (confirm auth behavior unchanged)**

Run: `cd api && uv run pytest tests/test_internal_org_closures.py -v`
Expected: all pass, including `test_rejects_when_secret_unconfigured`, `test_rejects_missing_signature`, `test_rejects_bad_signature` — the endpoint must reject exactly the same requests it did before, just via router-level wiring now.

- [ ] **Step 6: Lint**

Run: `cd api && uv run ruff check hailhq/api/routes/internal/org_closures.py tests/test_internal_org_closures.py`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add api/hailhq/api/routes/internal/org_closures.py api/tests/test_internal_org_closures.py
git commit -m "fix(api): wire org-closures auth dependency at router level, matching dsar.py"
```

---

### Task 5: Fix DSAR case-sensitivity in email address matching

**Files:**

- Modify: `core/hailhq/core/dsar.py:51-80` (`lookup_recipient`'s `Email` query)
- Test: `core/tests/test_dsar.py`

**Interfaces:**

- Consumes: `normalize_recipient` (`core/hailhq/core/compliance_gate.py`, unchanged).
- Produces: `lookup_recipient(session, identifier) -> DSARRecord` — same signature; the `Email` query now matches case-insensitively.

**Root cause:** `normalize_recipient()` fully lowercases email-shaped identifiers, but `Email.to_addresses`/`cc_addresses`/`bcc_addresses` preserve the original local-part casing at write time (only the domain is lowercased — see `schemas.py`'s `_normalize_domain`). The exact-match `.any(norm)` check therefore misses a stored address like `"Alice@example.com"` when a DSAR request comes in for `"alice@example.com"`, silently failing GDPR Article 17 erasure for that recipient's email content. The existing code comment promises a case-insensitive fallback that was never implemented.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_dsar.py`, right after the existing `test_lookup_recipient_normalizes_email_case` test (which only covers the _reverse_ direction — lowercase stored, uppercase query — and already passes):

```python
async def test_lookup_recipient_matches_mixed_case_stored_local_part(async_session):
    """The reverse direction of test_lookup_recipient_normalizes_email_case:
    the STORED address has a mixed-case local part (as a tenant might type
    it), and the DSAR request comes in fully lowercase (as a data subject
    would naturally type their own address)."""
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=["Alice@example.com"])
    await async_session.commit()

    record = await lookup_recipient(async_session, "alice@example.com")
    assert [e.id for e in record.emails] == [email.id]


async def test_delete_recipient_data_scrubs_mixed_case_stored_local_part(async_session):
    org_id = uuid.uuid4()
    email = await _make_email(async_session, org_id, to=["Alice@example.com"])
    await async_session.commit()

    summary = await delete_recipient_data(async_session, "alice@example.com")

    assert summary.emails_scrubbed == 1
    await async_session.refresh(email)
    assert email.body_text == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd core && uv run pytest tests/test_dsar.py::test_lookup_recipient_matches_mixed_case_stored_local_part tests/test_dsar.py::test_delete_recipient_data_scrubs_mixed_case_stored_local_part -v`
Expected: FAIL — both assert on an empty/zero result (the exact-match query finds nothing)

- [ ] **Step 3: Implement case-insensitive matching**

In `core/hailhq/core/dsar.py`, add `func` and `exists` to the existing `sqlalchemy` import (currently `from sqlalchemy import inspect as sa_inspect` and `from sqlalchemy import or_, select`):

```python
from sqlalchemy import exists, func, inspect as sa_inspect, or_, select
```

Add this helper right after the module docstring's imports, before `lookup_recipient`:

```python
def _array_contains_ci(column, norm: str):
    """Case-insensitive membership test against a Postgres text[] column.

    ``to_addresses``/``cc_addresses``/``bcc_addresses`` aren't lowercased
    at write time beyond the domain (see ``schemas.py``'s
    ``_normalize_domain``), so an exact-match ``.any(norm)`` misses a
    stored mixed-case local part. ``norm`` is already fully lowercased by
    the caller (``normalize_recipient``).
    """
    unnested = func.unnest(column).table_valued("addr")
    return exists(select(unnested.c.addr).where(func.lower(unnested.c.addr) == norm))
```

Replace the `emails = list(...)` block in `lookup_recipient` (currently lines 62-80):

```python
    # Case-insensitive match against stored addresses: to_addresses/cc/bcc
    # aren't lowercased at write time (see api/hailhq/api/routes/emails.py),
    # unlike suppressions.recipient.
    emails = list(
        (
            await session.execute(
                select(Email).where(
                    or_(
                        _array_contains_ci(Email.to_addresses, norm),
                        _array_contains_ci(Email.cc_addresses, norm),
                        _array_contains_ci(Email.bcc_addresses, norm),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/test_dsar.py -v`
Expected: all pass, including the 2 new tests and every pre-existing `test_lookup_recipient_*`/`test_delete_recipient_data_*` test (confirms the rewritten query still matches everything the old exact-match query matched, plus the mixed-case case).

- [ ] **Step 5: Lint**

Run: `cd core && uv run ruff check hailhq/core/dsar.py tests/test_dsar.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/dsar.py core/tests/test_dsar.py
git commit -m "fix(dsar): match stored email addresses case-insensitively"
```

---

### Task 6: Fix DSAR audit-log search to cover cc/bcc recipients

**Files:**

- Modify: `api/hailhq/api/routes/emails.py:320` (`email.blocked` audit payload), `api/hailhq/api/routes/emails.py:365-374` (`email.create` audit payload)
- Modify: `core/hailhq/core/dsar.py:92-110` (`lookup_recipient`'s `AuditLog` query — builds on Task 5's version of this function)
- Test: `core/tests/test_dsar.py`

**Interfaces:**

- Consumes: nothing new.
- Produces: no signature change — `AuditLog.payload` gains `"cc"`/`"bcc"` keys (additive; the existing `"to"` key's meaning is unchanged, so any other consumer of these audit payloads is unaffected). `lookup_recipient`'s audit-log query additionally checks those keys.

**Root cause:** `lookup_recipient`'s `AuditLog` query only matches `payload["to"]`. `emails.py`'s two audit writes (`email.blocked`, `email.create`) never include `cc`/`bcc` addresses under any key. A DSAR request for a cc'd- or bcc'd-only recipient finds the `Email` row itself (queried directly) but returns zero matching `audit_logs`, producing an incomplete export/deletion record for that recipient's audit trail.

- [ ] **Step 1: Write the failing test**

Add to `core/tests/test_dsar.py`:

```python
async def test_lookup_recipient_finds_audit_log_via_bcc(async_session):
    org_id = uuid.uuid4()
    audit = AuditLog(
        organization_id=org_id,
        action="email.create",
        resource_type="email",
        payload={
            "to": ["someone-else@example.com"],
            "cc": None,
            "bcc": [EMAIL_ADDR],
        },
    )
    async_session.add(audit)
    await async_session.commit()

    record = await lookup_recipient(async_session, EMAIL_ADDR)
    assert [a.id for a in record.audit_logs] == [audit.id]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_dsar.py::test_lookup_recipient_finds_audit_log_via_bcc -v`
Expected: FAIL — `record.audit_logs == []`

- [ ] **Step 3: Add cc/bcc to the audit payloads in `emails.py`**

In `api/hailhq/api/routes/emails.py`, change the `email.blocked` payload (currently line 320):

```python
            payload={"to": body.to, "reason": gate.reason, "checks": gate.checks},
```

to:

```python
            payload={
                "to": body.to,
                "cc": body.cc,
                "bcc": body.bcc,
                "reason": gate.reason,
                "checks": gate.checks,
            },
```

Change the `email.create` payload (currently lines 365-374):

```python
        payload={
            "from": email.from_address,
            "to": email.to_addresses,
            "subject": email.subject,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
```

to:

```python
        payload={
            "from": email.from_address,
            "to": email.to_addresses,
            "cc": email.cc_addresses,
            "bcc": email.bcc_addresses,
            "subject": email.subject,
            "recipient_consent": body.recipient_consent,
            "consent_source": body.consent_source,
            "consent_obtained_at": isoformat_or_none(body.consent_obtained_at),
            "message_type": body.message_type,
            "compliance": gate.checks,
        },
```

- [ ] **Step 4: Extend `lookup_recipient`'s AuditLog query**

In `core/hailhq/core/dsar.py`, replace the `audit_logs = list(...)` block (the one built on Task 5's file version — search for `AuditLog.payload["to"]`):

```python
    # audit_log payloads carry "to" as either a bare string (call.create /
    # call.blocked) or a list of addresses (email.create / email.blocked) —
    # see api/hailhq/api/routes/{calls,emails}.py. ``.astext`` covers the
    # scalar shape; JSONB containment (``@>``, via ``.contains``) covers
    # the array shape. "cc"/"bcc" are email-only, list-shaped, and absent
    # entirely from call payloads — a missing key evaluates to SQL NULL,
    # which safely doesn't match rather than erroring.
    audit_logs = list(
        (
            await session.execute(
                select(AuditLog).where(
                    or_(
                        AuditLog.payload["to"].astext == norm,
                        AuditLog.payload["to"].contains([norm]),
                        AuditLog.payload["cc"].contains([norm]),
                        AuditLog.payload["bcc"].contains([norm]),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_dsar.py -v`
Expected: all pass, including the new `test_lookup_recipient_finds_audit_log_via_bcc` and every pre-existing test (in particular `test_lookup_recipient_finds_suppressions_and_audit_log`, whose `AuditLog` row has no `"cc"`/`"bcc"` key at all — confirms a missing key doesn't error).

- [ ] **Step 6: Run the emails API suite (confirm the payload change doesn't break anything reading it)**

Run: `cd api && uv run pytest tests/test_emails_api.py -v`
Expected: all pass — no existing test asserts an exact/exhaustive audit payload shape that a new key would break (if one does, extend its expected dict with `"cc": ..., "bcc": ...` matching what the test's own email/body actually sends).

- [ ] **Step 7: Lint**

Run: `cd core && uv run ruff check hailhq/core/dsar.py tests/test_dsar.py && cd ../api && uv run ruff check hailhq/api/routes/emails.py`
Expected: no errors

- [ ] **Step 8: Commit**

```bash
git add core/hailhq/core/dsar.py core/tests/test_dsar.py api/hailhq/api/routes/emails.py
git commit -m "fix(dsar): include cc/bcc recipients in audit-log payloads and DSAR search"
```

---

### Task 7: Extract a shared HMAC signing/verification helper

**Files:**

- Create: `core/hailhq/core/hmac_signing.py`
- Modify: `core/hailhq/core/internal_webhook.py:34-37` (`_sign`, call site), `core/hailhq/core/providers/email/inbound/ses.py:24-38` (`SesInboundProvider`), `api/hailhq/api/routes/internal/auth.py:28-53` (`verify_internal_request`)
- Test: `core/tests/test_hmac_signing.py` (new)

**Interfaces:**

- Consumes: nothing.
- Produces: `sign(body: bytes, secret: str) -> str` (returns the `"sha256=<hex>"` header value), `verify(header: str | None, body: bytes, secret: str) -> bool` — both in `core/hailhq/core/hmac_signing.py`. `internal_webhook.py`, `ses.py`, and `auth.py` all delegate to these instead of each hand-rolling the construction.

**Root cause:** Three independent files implement the identical `hmac.new(secret, body, sha256).hexdigest()` / `"sha256=<hex>"` scheme with no shared helper: `internal_webhook.py`'s `_sign` (signing side), `ses.py`'s `SesInboundProvider.verify_notification` (verify side, using a byte-comparison form of `hmac.compare_digest`), and `auth.py`'s `verify_internal_request` (verify side, using a _string_-comparison form). This isn't just theoretical drift risk: `hmac.compare_digest` on two `str` arguments raises `TypeError` if either contains non-ASCII characters — `ses.py`'s byte-comparison form is immune to this (there's an existing regression test, `test_non_ascii_signature_is_rejected_not_500`, proving it), but `auth.py`'s string-comparison form is not, and has no equivalent test. Consolidating onto the byte-safe form fixes this live latent bug in `auth.py` as part of removing the duplication.

- [ ] **Step 1: Write the failing test**

Create `core/tests/test_hmac_signing.py`:

```python
"""Tests for the shared HMAC-SHA256-over-body signing/verification scheme
(`X-Hail-Signature: sha256=<hex>`), used by internal_webhook.py (signing),
ses.py, and api/hailhq/api/routes/internal/auth.py (verifying)."""

from __future__ import annotations

from hailhq.core.hmac_signing import sign, verify


def test_sign_produces_sha256_prefixed_hex():
    header = sign(b'{"a":1}', "s3cret")
    assert header.startswith("sha256=")
    assert len(header) == len("sha256=") + 64  # sha256 hex digest is 64 chars


def test_verify_accepts_a_correctly_signed_body():
    body = b'{"a":1}'
    header = sign(body, "s3cret")
    assert verify(header, body, "s3cret") is True


def test_verify_rejects_wrong_secret():
    body = b'{"a":1}'
    header = sign(body, "s3cret")
    assert verify(header, body, "wrong-secret") is False


def test_verify_rejects_tampered_body():
    header = sign(b'{"a":1}', "s3cret")
    assert verify(header, b'{"a":2}', "s3cret") is False


def test_verify_rejects_missing_header():
    assert verify(None, b'{"a":1}', "s3cret") is False
    assert verify("", b'{"a":1}', "s3cret") is False


def test_verify_rejects_wrong_prefix():
    assert verify("md5=deadbeef", b'{"a":1}', "s3cret") is False


def test_verify_rejects_non_ascii_signature_without_raising():
    """Regression: hmac.compare_digest on two `str` raises TypeError for
    non-ASCII input. verify() must compare bytes internally, not str, so
    a malformed header degrades to a clean False, not an unhandled
    exception (which would surface as a 500 from a FastAPI dependency)."""
    assert verify("sha256=héllo", b'{"a":1}', "s3cret") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_hmac_signing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hailhq.core.hmac_signing'`

- [ ] **Step 3: Create the shared module**

Create `core/hailhq/core/hmac_signing.py`:

```python
"""Shared HMAC-SHA256-over-body signing/verification.

The `X-Hail-Signature: sha256=<hex>` scheme used across every internal
auth boundary in this repo: `internal_webhook.py` signs voicebot/api →
hail-website calls; `providers/email/inbound/ses.py` verifies
Lambda → API SES notifications; `api/hailhq/api/routes/internal/auth.py`
verifies hail-website → API calls. One implementation so these three
call sites can't silently diverge on header parsing or comparison
semantics — see the byte- vs. str-comparison note on ``verify`` below.
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["sign", "verify"]


def sign(body: bytes, secret: str) -> str:
    """Return the `sha256=<hex>` header value for ``body`` signed with ``secret``."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(header: str | None, body: bytes, secret: str) -> bool:
    """Constant-time check that ``header`` is ``sign(body, secret)``.

    Compares as bytes, not str: ``hmac.compare_digest`` raises
    ``TypeError`` on non-ASCII ``str`` input, which would surface as an
    unhandled 500 from a caller expecting a clean ``False``.
    """
    if not header or not header.startswith("sha256="):
        return False
    provided = header.split("=", 1)[1]
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.encode(), expected.encode())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_hmac_signing.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Migrate `internal_webhook.py` to use `sign`**

In `core/hailhq/core/internal_webhook.py`, remove the `_sign` function (currently lines 34-37):

```python
def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"
```

Add `from hailhq.core.hmac_signing import sign` to the import block, and remove `import hashlib` and `import hmac` if nothing else in the file uses them (check with `grep -n "hashlib\.\|hmac\." core/hailhq/core/internal_webhook.py` after the edit — if either still appears elsewhere, keep that import).

Update the one call site (in `_post`, where it currently does `"X-Hail-Signature": _sign(body, secret)`) to:

```python
        "X-Hail-Signature": sign(body, secret),
```

- [ ] **Step 6: Migrate `ses.py` to use `verify`**

In `core/hailhq/core/providers/email/inbound/ses.py`, replace `SesInboundProvider.__init__` and `verify_notification` (currently lines 25-38):

```python
    def __init__(self, *, hmac_secret: str) -> None:
        if not hmac_secret:
            raise ValueError("SesInboundProvider requires a non-empty hmac_secret")
        self._secret = hmac_secret

    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        header = headers.get("X-Hail-Signature") or headers.get("x-hail-signature")
        return verify(header, body, self._secret)
```

(Note `self._secret` now stores the plain string, not pre-encoded bytes — `verify()` does the encoding.)

Add `from hailhq.core.hmac_signing import verify` to the import block, and remove `import hashlib` and `import hmac` (no longer used directly in this file after the edit — confirm with `grep -n "hashlib\.\|hmac\." core/hailhq/core/providers/email/inbound/ses.py`).

- [ ] **Step 7: Run the SES provider's existing tests**

Run: `cd core && uv run pytest tests/providers/email/inbound/test_ses.py -v`
Expected: all pass, including the pre-existing `test_non_ascii_signature_is_rejected_not_500`, `test_verify_notification_accepts_valid_signature`, `test_verify_notification_accepts_lowercase_header`, `test_verify_notification_rejects_bad_signature`, `test_verify_notification_rejects_missing_header`, `test_verify_notification_rejects_wrong_prefix`.

- [ ] **Step 8: Migrate `auth.py` to use `verify`**

Replace `api/hailhq/api/routes/internal/auth.py`'s body:

```python
"""Shared-secret auth for hail-website → hail internal endpoints.

Reuses ``HAIL_INTERNAL_SECRET`` — already the shared HMAC secret for
internal API↔website calls in the other direction (see
``hailhq.core.internal_webhook``, which signs voicebot/api → website
calls with it) — rather than minting a second secret for this direction.
Same scheme as everywhere else in this repo (``hailhq.core.hmac_signing``):
HMAC-SHA256 over the raw request body, sent as
``X-Hail-Signature: sha256=<hex>``.

Used by ``routes/internal/org_closures.py`` and ``routes/internal/dsar.py``.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi import status as http_status

from hailhq.core.config import settings
from hailhq.core.hmac_signing import verify

__all__ = ["verify_internal_request"]


async def verify_internal_request(request: Request) -> None:
    """FastAPI dependency: 503 if unconfigured, 401 on a bad/missing
    signature. Reads the raw body via ``request.body()``, which Starlette
    caches — the route's own Pydantic body model re-reads the same bytes."""
    secret = settings.hail_internal_secret
    if not secret:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="internal endpoint disabled: HAIL_INTERNAL_SECRET is unset",
        )

    body = await request.body()
    if not verify(request.headers.get("x-hail-signature"), body, secret):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )
```

- [ ] **Step 9: Add a regression test for the non-ASCII-header crash `auth.py` used to have**

Add to `api/tests/test_internal_dsar.py` (or `test_internal_org_closures.py` — either exercises the same `verify_internal_request` dependency; pick `test_internal_dsar.py` since it's the file the shared `_signed`/`internal_secret_set` helpers already live in):

```python
async def test_rejects_non_ascii_signature_with_401_not_500(
    client: httpx.AsyncClient, internal_secret_set
):
    resp = await client.post(
        "/internal/dsar/lookup",
        content=b'{"identifier": "+14155551234"}',
        headers={
            "X-Hail-Signature": "sha256=héllo",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401
```

- [ ] **Step 10: Run the full API + core test suites**

Run: `cd core && uv run pytest -q`
Expected: all pass, 0 failures

Run: `cd api && uv run pytest -q`
Expected: all pass, 0 failures (including the new non-ASCII regression test)

- [ ] **Step 11: Lint**

Run: `cd core && uv run ruff check hailhq/core/hmac_signing.py hailhq/core/internal_webhook.py hailhq/core/providers/email/inbound/ses.py tests/test_hmac_signing.py && cd ../api && uv run ruff check hailhq/api/routes/internal/auth.py tests/test_internal_dsar.py`
Expected: no errors

- [ ] **Step 12: Commit**

```bash
git add core/hailhq/core/hmac_signing.py core/hailhq/core/internal_webhook.py core/hailhq/core/providers/email/inbound/ses.py core/tests/test_hmac_signing.py api/hailhq/api/routes/internal/auth.py api/tests/test_internal_dsar.py
git commit -m "fix(core): extract shared HMAC sign/verify helper, fixing a non-ASCII-header crash in auth.py"
```

---

### Final verification (after all 7 tasks)

- [ ] Run: `cd sdk && uv run pytest -q && uv run ruff check .`
- [ ] Run: `cd core && uv run pytest -q && uv run ruff check .`
- [ ] Run: `cd api && uv run pytest -q && uv run ruff check .`
- [ ] Run: `cd mcp && uv run pytest -q` (unaffected by this plan — confirms no collateral breakage)
- [ ] Run: `cd voicebot && uv run pytest -q` (unaffected by this plan — confirms no collateral breakage)
- [ ] Run: `cd cli && go build ./... && go vet ./... && go test ./...`
- [ ] All green — nothing committed beyond what each task's own Step 9/commit did.
